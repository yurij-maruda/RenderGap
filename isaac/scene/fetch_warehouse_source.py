r"""Mirror the warehouse + Nova Carter USD closure into data/warehouse_source/.

    .usdvenv\Scripts\python isaac\scene\fetch_warehouse_source.py
    env\rg-python.bat isaac\scene\fetch_warehouse_source.py        (also works)

One tree, read by both engines: Isaac finds it as ISAACSIM_ASSET_ROOT\warehouse_source
and data\warehouse_payload\root_warehouse.usda payloads it as ..\warehouse_source.
Reading the same bytes on both sides is the premise of the whole benchmark -- a
second, separately collected copy for Isaac would put an unmeasured difference
inside the number this project exists to measure.

    data/warehouse_source/  writable working copy. What both engines read.

Unreal's USD importer has no notion of Isaac's asset root, so Pass 2B needs real
files on disk under a plain relative-path layout -- and it needs them hand
fixable, because the two importers disagree about enough small things
(asset-root-relative paths, MDL materials, missing subdivision tags) that some
of them have to be edited by hand before UE opens the stage.

Mechanism: HTTPS + OpenUSD only. No Kit, no ``omni.kit.usd.collect``, so it runs
in the ``usd-core`` venv in seconds. Starting from the root layers it walks every
sublayer, reference, payload, variant and asset-valued attribute, downloads each
dependency, and repeats until the closure is closed. The CDN directory layout is
mirrored exactly, so every relative reference inside the downloaded layers
resolves against the local copy unchanged.

Then it fixes the references that mirroring alone cannot. Isaac's layers author
asset-root-relative paths (``/Isaac/Materials/X.mdl``), which resolve only for a
resolver that has been told where the root is -- Isaac has ISAACSIM_ASSET_ROOT,
Unreal has nothing equivalent and drops the reference silently. Because the
layout is mirrored, each of those names the same file as a plain relative path
from the layer that authors it, and the relative spelling needs no configuration
in either engine. Every one is rewritten in place, then the file is re-read to
confirm none survived. ``--no-rewrite`` leaves them alone.

Re-running is safe. A file whose bytes match neither the CDN nor this script's
own rewrite is treated as hand-edited and kept, not clobbered -- that is the
whole point of this tree. ``--force`` overrides that and restores upstream.

Output: data/warehouse_source/ plus _fetch_manifest.json (provenance: source URL,
size and hash per file, every link rewritten, and every reference that could not
be resolved).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import posixpath
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from isaac.tools._bootstrap import ensure_pxr, repo_root  # noqa: E402

# Pinned, not read from ISAACSIM_ASSET_ROOT: this script is what *creates* the
# local root, so it must always source from upstream. The pack version is part of
# the pin -- bump it and every recorded hash in the manifest changes with it.
CDN_ROOT = "https://omniverse-content-production.s3-us-west-2.amazonaws.com/Assets/Isaac/6.0"

# nova_carter.usd is the full robot -- all Variants/ and Payloads/, sensors
# included. Nova_Carter_ROS.usd is the ROS 2 wrapper the Isaac pass actually
# drives. Both are seeded so this closure is a superset of what Pass 2A loads.
SOURCES = [
    "/Isaac/Environments/Simple_Warehouse/warehouse_multiple_shelves.usd",
    "/Isaac/Robots/NVIDIA/NovaCarter/nova_carter.usd",
    "/Isaac/Samples/ROS2/Robots/Nova_Carter_ROS.usd",
]

USD_EXT = {".usd", ".usda", ".usdc"}

# Isaac's materials reference these as if they sat next to the layer, but they
# ship inside Kit's MDL search path and are 404 on the CDN. Expected, not broken
# -- and moot for Pass 2B anyway, since Unreal reads the UsdPreviewSurface
# network rather than MDL.
KIT_CORE_MDL = {"OmniPBR.mdl", "OmniGlass.mdl", "OmniSurface.mdl", "OmniSurfacePresets.mdl", "OmniPBR_ClearCoat.mdl"}
TEXTURE_RE = re.compile(r'"([^"\n]+\.(?:png|jpg|jpeg|exr|hdr|dds|tif|tiff|tga|bmp))"', re.I)

# ``using .::OmniUe4Base import *;`` and ``import .::OmniUe4Base::*;`` -- both
# spellings of a *package-relative* MDL import, which means a module file
# sitting next to the importer. The leading dot is the whole discriminator:
# ``import ::OmniPBR`` (no dot) resolves from Kit's MDL root and must not match,
# and neither must the standard modules, which are all spelled ``::math`` &c.
MDL_MODULE_RE = re.compile(r"^[ \t]*(?:using|import)[ \t]+\.::([A-Za-z0-9_]+)", re.M)

MANIFEST_NAME = "_fetch_manifest.json"


# ---------------------------------------------------------------------------
# CDN keys
#
# A "key" is the path of a file relative to CDN_ROOT, e.g.
# "Isaac/Robots/NVIDIA/NovaCarter/nova_carter.usd". It is simultaneously the URL
# suffix and the path under the destination -- that identity is what makes every
# relative reference inside the layers keep working locally.
# ---------------------------------------------------------------------------

def key_to_url(key: str) -> str:
    return CDN_ROOT + "/" + urllib.parse.quote(key, safe="/")


def key_to_path(dest: str, key: str) -> str:
    return os.path.join(dest, *key.split("/"))


def resolve(raw: str, parent_key: str) -> tuple[str | None, str]:
    """Map one authored asset path to a key. Returns (key, reason-if-unresolved)."""
    path = raw.strip().replace("\\", "/")
    if not path or path.startswith("<") or path.startswith("anon:"):
        return None, "internal"

    scheme = urllib.parse.urlparse(path).scheme
    if scheme in ("http", "https"):
        if path.startswith(CDN_ROOT + "/"):
            return urllib.parse.unquote(path[len(CDN_ROOT) + 1:]), ""
        return None, "off-CDN absolute URL"
    if len(scheme) > 1:  # omniverse://, file://, ... -- but not a drive letter
        return None, f"{scheme}:// reference"
    if re.match(r"^[A-Za-z]:", path):
        return None, "absolute local path"

    if path.startswith("/"):
        # Asset-root-relative, the Isaac convention. Resolvable here because we
        # know the root -- but Unreal does not, so these are also recorded: they
        # are exactly the set rewrite_links() re-anchors once the crawl is done.
        return posixpath.normpath(path.lstrip("/")), ""

    joined = posixpath.normpath(posixpath.join(posixpath.dirname(parent_key), path))
    if joined.startswith(".."):
        return None, "escapes the asset root"
    return joined, ""


# ---------------------------------------------------------------------------
# Dependency extraction
# ---------------------------------------------------------------------------

def _asset_paths(value) -> list[str]:
    """Pull asset paths out of an attribute value of any shape."""
    from pxr import Sdf

    if isinstance(value, Sdf.AssetPath):
        return [value.path]
    if isinstance(value, dict):  # timeSamples
        out: list[str] = []
        for item in value.values():
            out.extend(_asset_paths(item))
        return out
    if isinstance(value, (list, tuple)) or type(value).__name__.endswith("Array"):
        try:
            return [v.path for v in value if isinstance(v, Sdf.AssetPath)]
        except TypeError:
            return []
    return []


def usd_deps(path: str) -> list[str]:
    """Every asset path authored *directly* in one layer.

    Deliberately one level deep and layer-local: UsdUtils.ComputeAllDependencies
    would resolve the whole closure in one call, but it can only follow
    references that already exist on disk, and here they exist only after we
    fetch them. So: parse, fetch, parse again.

    Variants are walked unselected, so a robot's sensor variants all come down,
    not just whichever one happens to be the default selection.
    """
    from pxr import Sdf

    layer = Sdf.Layer.FindOrOpen(path)
    if layer is None:
        raise RuntimeError("USD could not open the layer")

    out: list[str] = list(layer.subLayerPaths)

    def list_op_items(list_op) -> list:
        """Every item of a list op, whichever slot it was authored into.

        All five, not just GetAddedOrExplicitItems(): a payload authored as
        `prepend payloads` in a variant is still a file we have to fetch.
        The proxies do not concatenate, hence the per-slot list().
        """
        if list_op is None:
            return []
        items = []
        for slot in ("explicitItems", "addedItems", "prependedItems", "appendedItems", "orderedItems"):
            items.extend(list(getattr(list_op, slot, [])))
        return items

    def visit_prim(prim) -> None:
        for item in list_op_items(prim.referenceList) + list_op_items(prim.payloadList):
            if item.assetPath:
                out.append(item.assetPath)

        info_keys = prim.ListInfoKeys()
        if "clips" in info_keys:
            for entry in (prim.GetInfo("clips") or {}).values():
                out.extend(_asset_paths(dict(entry).get("assetPaths")))

        for prop in prim.properties:
            if not isinstance(prop, Sdf.AttributeSpec):
                continue
            keys = prop.ListInfoKeys()
            if "default" in keys:
                out.extend(_asset_paths(prop.default))
            if "timeSamples" in keys:
                out.extend(_asset_paths(prop.GetInfo("timeSamples")))

        for child in prim.nameChildren:
            visit_prim(child)
        for vset in prim.variantSets:
            for variant in vset.variants:
                visit_prim(variant.primSpec)

    for prim in layer.rootPrims:
        visit_prim(prim)

    return out


def mdl_deps(path: str) -> list[str]:
    """Textures and sibling modules referenced by an MDL module.

    USD cannot parse MDL, and Isaac's materials carry their maps as plain quoted
    paths inside the .mdl -- so this is a text scan, not a parse.

    Package-relative imports (``using .::OmniUe4Base import *;``) matter as much
    as the textures: every Simple_Warehouse material imports OmniUe4Base and
    OmniUe4Function from its own directory, and without those two files on disk
    all twelve fail to compile and the RTX viewport renders the whole warehouse
    in its error material. A bare ``OmniUe4Base.mdl`` is already the right thing
    to hand back -- resolve() anchors it to the referring module's directory,
    which is exactly what ``.::`` means.

    Absolute imports (``import ::OmniPBR``, ``import ::math::*``) are not
    followed: those resolve from Kit's MDL search path, not the CDN.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    return TEXTURE_RE.findall(text) + [f"{name}.mdl" for name in MDL_MODULE_RE.findall(text)]


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, path: str, retries: int = 3) -> int:
    """Stream one file to disk. Writes .part first so a killed run leaves no
    truncated layer that the next run would happily parse."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    request = urllib.request.Request(url, headers={"User-Agent": "rendergap-fetch/1"})
    last: Exception | None = None

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=60) as response, open(tmp, "wb") as fh:
                size = 0
                while True:
                    chunk = response.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    size += len(chunk)
            os.replace(tmp, path)
            return size
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
            last = exc
        time.sleep(1.5 * (attempt + 1))

    if os.path.exists(tmp):
        os.remove(tmp)
    raise RuntimeError(str(last))


class Fetcher:
    def __init__(self, dest: str, force: bool, jobs: int) -> None:
        self.dest = dest
        self.force = force
        self.jobs = jobs
        self.records: dict[str, dict] = {}            # key -> {url, size, sha256}
        self.edited: list[str] = []                   # kept: differ from upstream
        self.missing: list[str] = []                  # 404 on the CDN
        self.failed: list[str] = []
        self.unresolved: dict[str, list[str]] = {}    # "reason: path" -> referrers
        self.root_relative: dict[str, set[str]] = {}  # key -> the /Isaac/... it authors
        self.rewritten: dict[str, list[str]] = {}     # key -> "before -> after"
        self.unrewritten: dict[str, list[str]] = {}   # key -> paths the rewrite missed
        self.prior = self._load_prior()

    @staticmethod
    def is_core_mdl(key: str) -> bool:
        return os.path.basename(key) in KIT_CORE_MDL

    def _load_prior(self) -> dict[str, dict]:
        path = os.path.join(self.dest, MANIFEST_NAME)
        if not os.path.exists(path):
            return {}
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh).get("files", {})
        except (OSError, ValueError):
            return {}

    def fetch_one(self, key: str) -> tuple[str, str]:
        """Returns (key, status). Runs on a worker thread: I/O only, no USD."""
        path = key_to_path(self.dest, key)

        if os.path.exists(path) and not self.force:
            digest = sha256_of(path)
            prior = self.prior.get(key, {})
            known = prior.get("sha256")
            # Three ways the bytes on disk can be explained, and only the third
            # is a hand edit: they are what the CDN served; they are what *this*
            # script's link rewrite produced from what the CDN served; or they
            # are neither, which means a human (or bake_preview_surfaces.py)
            # changed them and they must not be clobbered.
            rewritten = prior.get("rewritten_sha256")
            edited = bool(known) and digest != known and digest != rewritten
            # For an edited file, size/sha256 keep describing *upstream* -- the
            # edit is reported on every later run instead of being absorbed into
            # the manifest by the first one -- and local_* describes the disk.
            record = {
                "url": key_to_url(key),
                "size": prior["size"] if edited or digest == rewritten else os.path.getsize(path),
                "sha256": known if edited or digest == rewritten else digest,
            }
            if edited:
                record["local_size"] = os.path.getsize(path)
                record["local_sha256"] = digest
            elif digest == rewritten:
                # Carry the rewrite forward so the next run recognises it too.
                record["rewritten_size"] = os.path.getsize(path)
                record["rewritten_sha256"] = digest
            self.records[key] = record
            return key, "edited" if edited else "cached"

        try:
            size = download(key_to_url(key), path)
        except urllib.error.HTTPError as exc:
            return key, "missing" if exc.code == 404 else f"failed: HTTP {exc.code}"
        except RuntimeError as exc:
            return key, f"failed: {exc}"

        self.records[key] = {"url": key_to_url(key), "size": size, "sha256": sha256_of(path)}
        return key, "new"

    @staticmethod
    def raw_paths(dest: str, key: str) -> list[str]:
        """Every asset path authored in one file, whatever kind of file it is.

        Split out of scan() so the rewrite pass can verify its own work by
        re-reading a layer exactly the way the crawler read it.
        """
        extension = os.path.splitext(key)[1].lower()
        path = key_to_path(dest, key)
        if extension in USD_EXT:
            return usd_deps(path)
        if extension == ".mdl":
            return mdl_deps(path)
        return []

    def scan(self, key: str) -> list[str]:
        """Direct dependencies of an already-downloaded file, as keys."""
        try:
            raw_paths = self.raw_paths(self.dest, key)
        except Exception as exc:  # one corrupt layer must not abort the mirror
            print(f"\n  [warn] cannot parse {key}: {exc}")
            return []

        out = []
        for raw in raw_paths:
            dep, reason = resolve(raw, key)
            if dep is None:
                if reason != "internal":
                    self.unresolved.setdefault(f"{reason}: {raw}", []).append(key)
                continue
            if raw.strip().startswith("/"):
                self.root_relative.setdefault(key, set()).add(raw)
            out.append(dep)
        return out

    def run(self, seeds: list[str]) -> None:
        queue = deque(seeds)
        seen = set(seeds)
        done = 0

        with ThreadPoolExecutor(max_workers=self.jobs) as pool:
            futures: dict = {}
            while queue or futures:
                while queue and len(futures) < self.jobs * 2:
                    key = queue.popleft()
                    futures[pool.submit(self.fetch_one, key)] = key

                finished, _ = wait(set(futures), return_when=FIRST_COMPLETED)
                for future in finished:
                    futures.pop(future)
                    key, status = future.result()
                    done += 1

                    if status == "missing":
                        self.missing.append(key)
                        continue
                    if status.startswith("failed"):
                        self.failed.append(f"{key}  ({status})")
                        continue
                    if status == "edited":
                        self.edited.append(key)

                    # Parsing stays on this thread: Sdf reads are cheap next to
                    # the downloads, and keeping USD single-threaded keeps the
                    # traversal above free of locking.
                    for dep in self.scan(key):
                        if dep not in seen:
                            seen.add(dep)
                            queue.append(dep)

                    # Anything that changed the tree scrolls; "cached" is noise
                    # and stays on one rewritten line.
                    quiet = status == "cached"
                    print(f"  [{done}/{len(seen)}] {status:7} {key}"[:120 if quiet else None],
                          end="\r" if quiet else "\n", flush=True)

    # -----------------------------------------------------------------------
    # Link rewriting
    # -----------------------------------------------------------------------

    def _relative_to(self, base: str, raw: str, changed: list[str]) -> str:
        """One asset-root-relative path, re-anchored to the layer that authors it."""
        stripped = raw.strip()
        if not stripped.startswith("/"):
            return raw
        target = posixpath.normpath(stripped.lstrip("/"))
        rel = posixpath.relpath(target, base) if base else target
        changed.append(f"{stripped} -> {rel}")
        return rel

    @staticmethod
    def _rewrite_mdl(path: str, modifier) -> None:
        """Substitute inside an .mdl, which USD cannot open and must be text-edited.

        surrogateescape both ways so a stray non-UTF-8 byte anywhere else in the
        module round-trips untouched instead of being replaced by U+FFFD, and
        newline="" so line endings survive verbatim. Only the quoted texture
        paths change.
        """
        with open(path, "r", encoding="utf-8", errors="surrogateescape", newline="") as fh:
            text = fh.read()

        def replace(match: "re.Match[str]") -> str:
            rewritten = modifier(match.group(1))
            return match.group(0) if rewritten == match.group(1) else f'"{rewritten}"'

        new_text = TEXTURE_RE.sub(replace, text)
        if new_text != text:
            with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as fh:
                fh.write(new_text)

    def rewrite_links(self) -> None:
        """Re-anchor every ``/Isaac/...`` reference to the layer that authors it.

        This is the half of "fetch it locally" that copying files does not do.
        An asset-root-relative path resolves only for a resolver that has been
        told where the root is: Isaac has ISAACSIM_ASSET_ROOT, Unreal has no
        equivalent and silently drops the reference. Since the mirror reproduces
        the CDN layout exactly, ``/Isaac/Materials/X.mdl`` authored inside
        ``Isaac/Environments/Simple_Warehouse/w.usd`` is the same file as
        ``../../Materials/X.mdl`` -- and the relative spelling resolves in both
        engines with no configuration at all.

        Only the paths the crawl already flagged are touched, so a layer with
        nothing root-relative in it is never rewritten and never even opened.

        Then it checks its own work. UsdUtils.ModifyAssetPaths is doing the
        traversal here rather than the hand-rolled walk in usd_deps(), and the
        two need not agree about where asset paths can hide -- a path only
        usd_deps() knows about (an unselected variant, a clips dict) would
        otherwise be silently left absolute. So every rewritten file is re-read
        with usd_deps()/mdl_deps() and anything still absolute is reported.
        """
        from pxr import Sdf, UsdUtils

        for key in sorted(self.root_relative):
            path = key_to_path(self.dest, key)
            if not os.path.exists(path) or key not in self.records:
                continue

            base = posixpath.dirname(key)
            changed: list[str] = []
            extension = os.path.splitext(key)[1].lower()

            try:
                if extension in USD_EXT:
                    layer = Sdf.Layer.FindOrOpen(path)
                    if layer is None:
                        raise RuntimeError("USD could not open the layer")
                    UsdUtils.ModifyAssetPaths(layer, lambda raw: self._relative_to(base, raw, changed))
                    if changed:
                        layer.Save()
                elif extension == ".mdl":
                    self._rewrite_mdl(path, lambda raw: self._relative_to(base, raw, changed))
                else:
                    continue
            except Exception as exc:  # a layer that will not rewrite is reported, not fatal
                print(f"  [warn] cannot rewrite {key}: {exc}")
                self.unrewritten[key] = sorted(self.root_relative[key])
                continue

            if not changed:
                continue

            self.rewritten[key] = sorted(set(changed))
            # The bytes are no longer what the CDN served. Record the new hash so
            # the next run reads this as "cached" rather than as a hand edit.
            self.records[key]["rewritten_size"] = os.path.getsize(path)
            self.records[key]["rewritten_sha256"] = sha256_of(path)

            try:
                leftover = {raw.strip() for raw in self.raw_paths(self.dest, key) if raw.strip().startswith("/")}
            except Exception as exc:
                print(f"  [warn] cannot verify {key}: {exc}")
                continue
            if leftover:
                self.unrewritten[key] = sorted(leftover)

    def write_manifest(self, sources: list[str]) -> str:
        path = os.path.join(self.dest, MANIFEST_NAME)
        payload = {
            "cdn_root": CDN_ROOT,
            "sources": sources,
            "fetched_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "file_count": len(self.records),
            "total_bytes": sum(record["size"] for record in self.records.values()),
            "locally_modified": sorted(self.edited),
            "missing_on_cdn": sorted(key for key in self.missing if not self.is_core_mdl(key)),
            "kit_core_mdl": sorted(key for key in self.missing if self.is_core_mdl(key)),
            "root_relative_references": sorted(
                f"{key} -> {raw}" for key, raws in self.root_relative.items() for raw in raws
            ),
            "links_rewritten": {key: self.rewritten[key] for key in sorted(self.rewritten)},
            "links_not_rewritten": {key: self.unrewritten[key] for key in sorted(self.unrewritten)},
            "unresolved_references": {
                label: sorted(set(referrers)) for label, referrers in sorted(self.unresolved.items())
            },
            "files": dict(sorted(self.records.items())),
        }
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, indent=2)
            fh.write("\n")
        return path


def main() -> int:
    default_dest = os.path.join(repo_root(), "data", "warehouse_source")

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dest", default=default_dest, help=f"Destination tree (default: {default_dest}).")
    parser.add_argument(
        "--source",
        action="append",
        metavar="/Isaac/...",
        help="Root layer to start from, CDN-root-relative. Repeatable. Replaces the defaults.",
    )
    parser.add_argument("--force", action="store_true", help="Re-download everything, discarding local edits.")
    parser.add_argument("--jobs", type=int, default=8, help="Parallel downloads (default: 8).")
    parser.add_argument(
        "--no-rewrite",
        action="store_true",
        help="Leave /Isaac/... references absolute. They resolve in Isaac but not in Unreal.",
    )
    args = parser.parse_args()

    backend = ensure_pxr()
    sources = args.source or SOURCES
    seeds = [source.lstrip("/") for source in sources]

    print(f"[fetch] usd backend : {backend}")
    print(f"[fetch] cdn root    : {CDN_ROOT}")
    print(f"[fetch] destination : {args.dest}")
    for source in sources:
        print(f"[fetch] source      : {source}")
    print()

    os.makedirs(args.dest, exist_ok=True)
    fetcher = Fetcher(args.dest, args.force, max(1, args.jobs))
    fetcher.run(seeds)

    # After the crawl, never during it: rewriting changes the bytes a file's
    # hash was taken from, and the traversal must see the tree as downloaded.
    if fetcher.root_relative and not args.no_rewrite:
        print(f"\n[fetch] rewriting links in {len(fetcher.root_relative)} file(s)...")
        fetcher.rewrite_links()

    manifest = fetcher.write_manifest(sources)
    total = sum(record["size"] for record in fetcher.records.values())
    print(f"\n[fetch] {len(fetcher.records)} files, {total / 1e9:.2f} GB -> {args.dest}")
    print(f"[fetch] manifest: {manifest}")

    if fetcher.edited:
        print(f"[fetch] {len(fetcher.edited)} file(s) kept because they differ from upstream (hand-edited):")
        for key in fetcher.edited[:10]:
            print(f"    {key}")
        print("[fetch] --force restores them from the CDN.")

    total_root_relative = sum(len(raws) for raws in fetcher.root_relative.values())
    if total_root_relative and args.no_rewrite:
        print(f"[fetch] {total_root_relative} asset-root-relative reference(s) left as-is (--no-rewrite).")
        print("[fetch] they resolve in Isaac but NOT in Unreal; listed in the manifest.")
    elif total_root_relative:
        rewritten = sum(len(entries) for entries in fetcher.rewritten.values())
        print(f"[fetch] {rewritten} asset-root-relative reference(s) rewritten relative "
              f"across {len(fetcher.rewritten)} file(s).")

    if fetcher.unrewritten:
        count = sum(len(paths) for paths in fetcher.unrewritten.values())
        print(f"[fetch] WARNING -- {count} reference(s) in {len(fetcher.unrewritten)} file(s) "
              f"are still absolute after the rewrite:")
        for key in list(fetcher.unrewritten)[:10]:
            print(f"    {key}: {', '.join(fetcher.unrewritten[key][:3])}")
        print("[fetch] Unreal will drop these; see links_not_rewritten in the manifest.")

    if fetcher.unresolved:
        print(f"[fetch] {len(fetcher.unresolved)} unresolved reference(s):")
        for label in list(fetcher.unresolved)[:10]:
            print(f"    {label}")

    core_mdl = [key for key in fetcher.missing if Fetcher.is_core_mdl(key)]
    absent = [key for key in fetcher.missing if not Fetcher.is_core_mdl(key)]

    if core_mdl:
        print(f"[fetch] {len(core_mdl)} MDL module(s) live in Kit, not on the CDN -- expected, ignore.")

    if absent:
        print(f"[fetch] {len(absent)} referenced file(s) are 404 on the CDN:")
        for key in absent[:10]:
            print(f"    {key}")

    if fetcher.failed:
        print(f"[fetch] FAILED -- {len(fetcher.failed)} download(s) did not complete:")
        for line in fetcher.failed[:10]:
            print(f"    {line}")
        return 1

    print("[fetch] OK -- open these in Unreal:")
    for seed in seeds:
        print(f"    {key_to_path(args.dest, seed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
