r"""Add a UsdPreviewSurface fallback to every MDL-only material in data/warehouse_source/.

    .usdvenv\Scripts\python isaac\scene\bake_preview_surfaces.py --dry-run
    env\rg-python.bat isaac\scene\bake_preview_surfaces.py        (also works)

Why this exists
---------------
The NVIDIA warehouse and Nova Carter assets bind materials *only* through the
``mdl`` render context::

    outputs:mdl:surface -> Shader(info:mdl:sourceAsset = @../Materials/MI_...mdl@)

There is no ``outputs:surface``. Isaac is happy -- RTX prefers the MDL context --
but Unreal's USD translator reads the *universal* context, finds no surface
source, and falls every mesh back to ``/USDCore/Materials/DisplayColor``. That is
the "everything imported white" symptom.

Unreal cannot read MDL in a stock build: the MDL schema translator is compiled
out unless the engine was built against NVIDIA's binary MDL SDK, whose distiller
component is excluded from the open-source release. So the fix belongs in the
scene, not the engine.

What it does
------------
Two steps per material.

1. *Add* ``outputs:surface`` and a small UsdPreviewSurface network beside the
   existing MDL shader.
2. *Block* ``outputs:mdl:surface`` -- author ``.connect = None`` -- so both engines
   resolve the same preview surface. The MDL shader prim and all of its inputs stay
   in the layer, untouched; only the one connection is cut, and ``--restore-mdl``
   puts it back.

Step 2 is why this is a benchmark tool and not just an import fix. RTX exposes no
setting to choose a render context -- it is MDL-native and binds
``outputs:mdl:surface`` whenever it is connected -- but ``UsdShadeMaterial`` falls
back to the universal context when the requested one has no connected source. So
cutting that single connection moves Isaac onto the same description Unreal reads,
verified::

    before   universal -> PreviewSurface     after   universal -> PreviewSurface
    before   mdl       -> MI_CeilingA_06b    after   mdl       -> PreviewSurface

That matters because the conversion below is lossy for 30 of 638 materials. Left
on one side only, that loss is *differential*: it lands in the Isaac-vs-Unreal
delta as a difference this script authored, indistinguishable from a real renderer
difference. Applied to both, it is *common-mode* and cancels. Pass ``--keep-mdl``
for the other arm, where Isaac renders the original MDL and the delta includes the
full USD transfer loss.

No MDL is parsed. Every value the preview network needs -- texture paths, tints,
roughness bounds, tiling -- is already authored as plain USD ``inputs:`` on the
MDL shader prim. The ``.mdl`` files only fix the *wiring* per module, and that
wiring is transcribed into RECIPES below, one entry per module. Where a module
authors nothing (its textures are baked into the MDL body), the recipe carries
the literal defaults read out of the ``.mdl`` source, resolved relative to the
module's own path.

The UE4-exported family (``OmniUe4Base``) maps onto UsdPreviewSurface exactly,
not approximately, because these were UE4 materials before they were MDL:

    Metallic  = ORM.b                          -> metallic  <- UsdUVTexture.b
    Roughness = lerp(RMin, RMax, ORM.g)        -> roughness <- UsdUVTexture.g

and that lerp is ``g * (RMax - RMin) + RMin``, which folds exactly onto the
per-channel ``inputs:scale`` / ``inputs:bias`` of a single UsdUVTexture. The MDL
flips V twice (``1-v`` when building the UV, ``1-v`` again at lookup), so the net
transform is identity and a plain ``st`` reader is correct.

The cases that do *not* map are reported at the end rather than silently
approximated -- see NOTE_* below.

Where it writes
---------------
Into the layers that actually author the materials, not into an overlay. The
2103 composed materials in the scene come from only ~380 authored specs across
~67 layers, because the props are referenced repeatedly -- so this is both less
work and instancing-safe. Authoring in place also means the relative texture
paths (``../Materials/Textures/T_BeamsA_D.png``) carry over verbatim.

Re-running is safe: a material that already has ``outputs:surface`` is skipped
unless ``--force`` is given. Touched files are recorded in the ``locally_modified``
list of ``_fetch_manifest.json``, which is how fetch_warehouse_source.py knows not
to clobber them.
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys
from collections import Counter

REPO = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from isaac.tools._bootstrap import ensure_pxr, repo_root  # noqa: E402

ensure_pxr()

from pxr import Gf, Sdf, Usd, UsdShade  # noqa: E402

PREFIX = "[rendergap]"

USD_SUFFIXES = (".usd", ".usda", ".usdc")

# Prim names for the network we author. Prefixed so they cannot collide with the
# MDL shader prim, which is named after its module (e.g. "MI_CeilingA_06b").
N_SURFACE = "PreviewSurface"
N_ST = "PreviewST"
N_UV = "PreviewUV"
N_ALBEDO = "PreviewAlbedo"
N_NORMAL = "PreviewNormal"
N_ORM = "PreviewORM"
N_ROUGH = "PreviewRoughness"
N_METAL = "PreviewMetallic"
N_OPACITY = "PreviewOpacity"

# UsdPreviewSurface's own defaults, used when a recipe supplies nothing. Close
# enough to OmniPBR's (diffuse 0.2 grey, roughness 0.5, metallic 0.0) that an
# unauthored input needs no special case.
PS_DEFAULT_ROUGHNESS = 0.5
PS_DEFAULT_METALLIC = 0.0

# Approximations, reported per-material at the end so they are a decision and not
# a silent loss.
NOTE_MASK_LERP = (
    "MaskSelection lerp(ColorAlbedo, AlbedoTexture, mask) has no UsdPreviewSurface "
    "equivalent; used the albedo texture alone"
)
NOTE_TRIPLE_MASK = (
    "Body/Cap/Handle 3-way mask select has no UsdPreviewSurface equivalent; used "
    "the albedo texture alone"
)
NOTE_TEXT_OVERLAY = (
    "per-channel (Text >= 0.5 ? a : b) select has no UsdPreviewSurface equivalent; "
    "used the base albedo without the text overlay"
)
NOTE_DESATURATION = "Desaturation is authored but not representable; ignored"


def _t(*names):
    """First authored input among `names`, as a plain 'read this input' recipe slot."""
    return tuple(names)


# One entry per MDL module. Keys are all optional:
#
#   albedo_tex/normal_tex/orm_tex/rough_tex/metal_tex/opacity_tex
#                    input names holding an asset path
#   *_literal        path relative to the .mdl module, for modules that bake the
#                    texture into the MDL body instead of exposing a parameter
#   albedo_tint      input holding a colour multiplier (Vec3f or Vec4f)
#   albedo_const     input holding a flat base colour, used when no texture
#   rough_min/max    inputs bounding the ORM.g remap
#   rough_const/metal_const   inputs holding scalar values
#   tiling_vec4      input whose .xy is the UV scale (MainTiling)
#   tiling_uv        pair of scalar inputs (U_Tiling, V_Tiling)
#   tiling_vec2      Vec2f input (OmniPBR texture_scale)
#   const            literal fallbacks applied last
#   note             approximation to report
#
# Sources: the .mdl files under Simple_Warehouse/Materials/ and NovaCarter/Materials/,
# and kit/mdl/core/Base/OmniPBR.mdl for the OmniPBR defaults.
RECIPES = {
    # -- OmniPBR family. 1666 of the 2103 materials in the scene. ---------------
    "OmniPBR.mdl": {
        "albedo_tex": _t("diffuse_texture"),
        "albedo_tint": _t("diffuse_tint"),
        "albedo_const": _t("diffuse_color_constant"),
        "normal_tex": _t("normalmap_texture"),
        "orm_tex": _t("ORM_texture"),
        "orm_enable": _t("enable_ORM_texture"),
        "rough_tex": _t("reflectionroughness_texture"),
        "rough_tex_influence": _t("reflection_roughness_texture_influence"),
        "rough_const": _t("reflection_roughness_constant"),
        "metal_tex": _t("metallic_texture"),
        "metal_tex_influence": _t("metallic_texture_influence"),
        "metal_const": _t("metallic_constant"),
        "opacity_tex": _t("opacity_texture"),
        "opacity_enable": _t("enable_opacity"),
        "emissive_color": _t("emissive_color"),
        "emissive_intensity": _t("emissive_intensity"),
        "emissive_enable": _t("enable_emission"),
        "tiling_vec2": _t("texture_scale"),
    },
    # Iron/Gold are OmniPBR presets and share its parameter names exactly.
    "Iron.mdl": "OmniPBR.mdl",
    "Gold.mdl": {
        # Authors nothing; these are the constants baked into Gold.mdl itself.
        "const": {"diffuseColor": (0.2, 0.2, 0.2), "roughness": 0.5, "metallic": 0.5},
    },
    # -- OmniUe4Base family, ORM + MaskSelection -------------------------------
    "MI_CeilingA_06b.mdl": {
        "albedo_tex": _t("AlbedoTexture"),
        "normal_tex": _t("MainNormalInput"),
        "orm_tex": _t("MergeMapInput"),
        "rough_min": _t("RoughnessMin"),
        "rough_max": _t("RoughnessMax"),
        "tiling_vec4": _t("MainTiling"),
        "note": NOTE_MASK_LERP,
    },
    "MI_FrameA_01.mdl": "MI_CeilingA_06b.mdl",
    "MI_Floor_01.mdl": "MI_CeilingA_06b.mdl",
    "MI_WallB_01.mdl": "MI_CeilingA_06b.mdl",
    "MI_PushcartA_01.mdl": {
        "albedo_tex": _t("AlbedoTexture"),
        "normal_tex": _t("MainNormalInput"),
        "orm_tex": _t("MergeMapInput"),
        "rough_min": _t("RoughnessMin"),
        "rough_max": _t("RoughnessMax"),
        "note": NOTE_TRIPLE_MASK,
    },
    # -- OmniUe4Base family, ORM + tint, no mask -------------------------------
    "MI_LampCeilingA.mdl": {
        "albedo_tex": _t("AlbedoTexture"),
        "albedo_tint": _t("BaseColor_Tint"),
        "normal_tex": _t("MainNormalInput"),
        "orm_tex": _t("MergeMapInput"),
        "rough_min": _t("RoughnessMin"),
        "rough_max": _t("RoughnessMax"),
        "tiling_uv": _t("U_Tiling", "V_Tiling"),
        "note_if_authored": ("Desaturation", NOTE_DESATURATION),
    },
    "MI_RackShield_01.mdl": "MI_LampCeilingA.mdl",
    # -- OmniUe4Base family, ORM + alpha mask (floor decals) -------------------
    "M_WallBoard_01.mdl": {
        "albedo_tex": _t("AlbedoTexture"),
        "normal_tex": _t("MainNormalInput"),
        "orm_tex": _t("MergeMapInput"),
        "rough_min": _t("RoughnessMin"),
        "rough_max": _t("RoughnessMax"),
        "opacity_tex": _t("AlphaSelection"),
        "opacity_threshold": 0.5,
    },
    # -- Flat constants --------------------------------------------------------
    "MI_Barcode_0001.mdl": {
        "albedo_tex": _t("BaseColor_Texture"),
        "albedo_tint": _t("BaseColor_Tint"),
        "rough_const": _t("Roughness"),
        "metal_const": _t("Metallic"),
    },
    # -- Modules that bake their textures into the MDL body --------------------
    "MI_SignB.mdl": {
        "albedo_tex": _t("TextureSelection"),
        "albedo_tex_literal": "./Textures/T_SignsA_D.png",
        "const": {"roughness": 0.125, "metallic": 0.0},
    },
    "M_AisleSign.mdl": {
        "albedo_tex_literal": "./Textures/T_AisleSign_D.png",
        "normal_tex_literal": "./Textures/T_AisleSign_N.png",
        "orm_tex_literal": "./Textures/T_AisleSign_ORM.png",
        "note": NOTE_TEXT_OVERLAY,
    },
    "M_Glow.mdl": {
        # EmissiveColor float4(0.28835, 0.365, 0.365) * EmissiveStrength 10.0,
        # base colour black, from M_Glow.mdl.
        "const": {
            "diffuseColor": (0.0, 0.0, 0.0),
            "emissiveColor": (2.8835, 3.65, 3.65),
            "roughness": 0.5,
            "metallic": 0.0,
        },
    },
}


def recipe_for(module: str):
    """Resolve a module name to its recipe, following string aliases."""
    r = RECIPES.get(module)
    while isinstance(r, str):
        r = RECIPES.get(r)
    return r


# --------------------------------------------------------------------------- #
# reading the MDL shader
# --------------------------------------------------------------------------- #


def read_input(shader: UsdShade.Shader, names):
    """First authored value among `names`, or None. Recipes list synonyms in order."""
    if not names:
        return None
    for name in names:
        inp = shader.GetInput(name)
        if inp:
            value = inp.Get()
            if value is not None:
                return value
    return None


def as_rgb(value):
    """Vec3f/Vec4f/float -> a 3-tuple, so tints and constants share one path."""
    if value is None:
        return None
    if isinstance(value, (Gf.Vec4f, Gf.Vec4d)):
        return (value[0], value[1], value[2])
    if isinstance(value, (Gf.Vec3f, Gf.Vec3d)):
        return (value[0], value[1], value[2])
    if isinstance(value, (int, float)):
        return (float(value),) * 3
    return None


def literal_asset(mdl_asset: str, relative: str) -> str:
    """Resolve a path baked into an .mdl body, relative to that module.

    The material prim can live in any layer, but a path like "./Textures/T_X.png"
    inside MI_SignB.mdl is relative to the *module*. Since info:mdl:sourceAsset is
    itself authored relative to the layer we are editing, joining the two yields a
    path that resolves correctly from that same layer.
    """
    base = posixpath.dirname(mdl_asset.replace("\\", "/"))
    joined = posixpath.join(base, relative.lstrip("./"))
    return posixpath.normpath(joined)


# --------------------------------------------------------------------------- #
# authoring the preview network
# --------------------------------------------------------------------------- #


class NetworkBuilder:
    """Creates UsdUVTexture / reader / transform prims under one material, lazily.

    Lazily because most materials use two or three textures out of the six slots,
    and an unused UsdPrimvarReader left behind would still be translated by UE.
    """

    def __init__(self, stage, material: UsdShade.Material, tiling):
        self.stage = stage
        self.material = material
        self.path = material.GetPath()
        self.tiling = tiling
        self._st_output = None

    def _uv_source(self):
        """The float2 output every texture's `st` connects to, created on demand."""
        if self._st_output is not None:
            return self._st_output

        reader = UsdShade.Shader.Define(self.stage, self.path.AppendChild(N_ST))
        reader.CreateIdAttr("UsdPrimvarReader_float2")
        # 'st' is the primary UV set on 1886 of the 1901 meshes in this scene; the
        # extras (st1, st2, vc, vc1) are lightmap and vertex-colour sets the MDL
        # never samples.
        reader.CreateInput("varname", Sdf.ValueTypeNames.Token).Set("st")
        out = reader.CreateOutput("result", Sdf.ValueTypeNames.Float2)

        if self.tiling and (abs(self.tiling[0] - 1.0) > 1e-6 or abs(self.tiling[1] - 1.0) > 1e-6):
            xform = UsdShade.Shader.Define(self.stage, self.path.AppendChild(N_UV))
            xform.CreateIdAttr("UsdTransform2d")
            xform.CreateInput("in", Sdf.ValueTypeNames.Float2).ConnectToSource(out)
            xform.CreateInput("scale", Sdf.ValueTypeNames.Float2).Set(
                Gf.Vec2f(self.tiling[0], self.tiling[1])
            )
            out = xform.CreateOutput("result", Sdf.ValueTypeNames.Float2)

        self._st_output = out
        return out

    def texture(self, name, asset, srgb, scale=None, bias=None):
        """A UsdUVTexture prim reading `asset`, wired to the shared UV source."""
        tex = UsdShade.Shader.Define(self.stage, self.path.AppendChild(name))
        tex.CreateIdAttr("UsdUVTexture")
        tex.CreateInput("file", Sdf.ValueTypeNames.Asset).Set(asset)
        tex.CreateInput("st", Sdf.ValueTypeNames.Float2).ConnectToSource(self._uv_source())
        # Every lookup in these modules is tex::wrap_repeat.
        tex.CreateInput("wrapS", Sdf.ValueTypeNames.Token).Set("repeat")
        tex.CreateInput("wrapT", Sdf.ValueTypeNames.Token).Set("repeat")
        tex.CreateInput("sourceColorSpace", Sdf.ValueTypeNames.Token).Set("sRGB" if srgb else "raw")
        if scale is not None:
            tex.CreateInput("scale", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(*scale))
        if bias is not None:
            tex.CreateInput("bias", Sdf.ValueTypeNames.Float4).Set(Gf.Vec4f(*bias))
        return tex


def build_preview(stage, material: UsdShade.Material, shader: UsdShade.Shader, recipe, mdl_asset):
    """Author outputs:surface + its network. Returns a list of notes to report."""
    notes = []

    def asset_for(tex_key):
        """A texture slot's asset path, from an authored input or an MDL literal."""
        value = read_input(shader, recipe.get(tex_key))
        if value is not None and getattr(value, "path", ""):
            return value.path
        literal = recipe.get(tex_key + "_literal")
        if literal:
            return literal_asset(mdl_asset, literal)
        return None

    # -- UV tiling ---------------------------------------------------------- #
    tiling = None
    vec4 = read_input(shader, recipe.get("tiling_vec4"))
    if vec4 is not None:
        tiling = (vec4[0], vec4[1])
    vec2 = read_input(shader, recipe.get("tiling_vec2"))
    if vec2 is not None:
        tiling = (vec2[0], vec2[1])
    uv_pair = recipe.get("tiling_uv")
    if uv_pair:
        u = read_input(shader, _t(uv_pair[0]))
        v = read_input(shader, _t(uv_pair[1]))
        if u is not None or v is not None:
            tiling = (u if u is not None else 1.0, v if v is not None else 1.0)

    net = NetworkBuilder(stage, material, tiling)
    surface = UsdShade.Shader.Define(stage, material.GetPath().AppendChild(N_SURFACE))
    surface.CreateIdAttr("UsdPreviewSurface")

    const = recipe.get("const", {})

    # -- base colour -------------------------------------------------------- #
    tint = as_rgb(read_input(shader, recipe.get("albedo_tint")))
    albedo = asset_for("albedo_tex")
    if albedo:
        # A tint folds into the texture's per-channel scale, so no multiply node.
        scale = (tint[0], tint[1], tint[2], 1.0) if tint else None
        tex = net.texture(N_ALBEDO, albedo, srgb=True, scale=scale)
        surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).ConnectToSource(
            tex.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        )
    else:
        flat = as_rgb(read_input(shader, recipe.get("albedo_const"))) or const.get("diffuseColor")
        if flat:
            if tint:
                flat = (flat[0] * tint[0], flat[1] * tint[1], flat[2] * tint[2])
            surface.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*flat))

    # -- normal ------------------------------------------------------------- #
    normal = asset_for("normal_tex")
    if normal:
        # Tangent-space normals arrive as [0,1] and UsdPreviewSurface wants [-1,1].
        tex = net.texture(
            N_NORMAL, normal, srgb=False, scale=(2, 2, 2, 1), bias=(-1, -1, -1, 0)
        )
        surface.CreateInput("normal", Sdf.ValueTypeNames.Normal3f).ConnectToSource(
            tex.CreateOutput("rgb", Sdf.ValueTypeNames.Float3)
        )

    # -- roughness + metallic ----------------------------------------------- #
    orm = asset_for("orm_tex")
    orm_enabled = read_input(shader, recipe.get("orm_enable"))
    if orm and orm_enabled is False:
        orm = None

    if orm:
        # ORM packs occlusion/roughness/metallic into r/g/b. The MDL remaps only
        # the green channel -- lerp(min, max, g) == g*(max-min) + min -- and
        # UsdUVTexture's scale/bias are per-channel, so one node serves both
        # roughness and metallic with blue left untouched.
        r_min = read_input(shader, recipe.get("rough_min"))
        r_max = read_input(shader, recipe.get("rough_max"))
        if r_min is not None and r_max is not None:
            scale = (1.0, r_max - r_min, 1.0, 1.0)
            bias = (0.0, r_min, 0.0, 0.0)
        else:
            scale = bias = None
        tex = net.texture(N_ORM, orm, srgb=False, scale=scale, bias=bias)
        surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).ConnectToSource(
            tex.CreateOutput("g", Sdf.ValueTypeNames.Float)
        )
        surface.CreateInput("metallic", Sdf.ValueTypeNames.Float).ConnectToSource(
            tex.CreateOutput("b", Sdf.ValueTypeNames.Float)
        )
        # Occlusion is the red channel, and UsdPreviewSurface has a slot for it.
        surface.CreateInput("occlusion", Sdf.ValueTypeNames.Float).ConnectToSource(
            tex.CreateOutput("r", Sdf.ValueTypeNames.Float)
        )
    else:
        # OmniPBR keeps roughness and metallic in separate single-channel maps,
        # each with an "influence" blend against the constant. A blend cannot be
        # expressed, so the dominant side wins.
        rough_tex = asset_for("rough_tex")
        influence = read_input(shader, recipe.get("rough_tex_influence"))
        if rough_tex and (influence is None or influence >= 0.5):
            tex = net.texture(N_ROUGH, rough_tex, srgb=False)
            surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).ConnectToSource(
                tex.CreateOutput("r", Sdf.ValueTypeNames.Float)
            )
        else:
            value = read_input(shader, recipe.get("rough_const"))
            value = const.get("roughness", value)
            if value is not None and abs(value - PS_DEFAULT_ROUGHNESS) > 1e-6:
                surface.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(float(value))

        metal_tex = asset_for("metal_tex")
        influence = read_input(shader, recipe.get("metal_tex_influence"))
        if metal_tex and (influence is None or influence >= 0.5):
            tex = net.texture(N_METAL, metal_tex, srgb=False)
            surface.CreateInput("metallic", Sdf.ValueTypeNames.Float).ConnectToSource(
                tex.CreateOutput("r", Sdf.ValueTypeNames.Float)
            )
        else:
            value = read_input(shader, recipe.get("metal_const"))
            value = const.get("metallic", value)
            if value is not None and abs(value - PS_DEFAULT_METALLIC) > 1e-6:
                surface.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(float(value))

    # -- opacity ------------------------------------------------------------ #
    opacity = asset_for("opacity_tex")
    opacity_enabled = read_input(shader, recipe.get("opacity_enable"))
    if opacity and opacity_enabled is not False:
        tex = net.texture(N_OPACITY, opacity, srgb=False)
        surface.CreateInput("opacity", Sdf.ValueTypeNames.Float).ConnectToSource(
            tex.CreateOutput("r", Sdf.ValueTypeNames.Float)
        )
        threshold = recipe.get("opacity_threshold")
        if threshold is not None:
            # Gives UE a masked material rather than a translucent one, which is
            # what these decals want.
            surface.CreateInput("opacityThreshold", Sdf.ValueTypeNames.Float).Set(threshold)

    # -- emission ----------------------------------------------------------- #
    emissive = as_rgb(read_input(shader, recipe.get("emissive_color"))) or const.get("emissiveColor")
    if emissive:
        enabled = read_input(shader, recipe.get("emissive_enable"))
        if enabled is not False:
            intensity = read_input(shader, recipe.get("emissive_intensity"))
            if intensity:
                emissive = tuple(c * intensity for c in emissive)
            surface.CreateInput("emissiveColor", Sdf.ValueTypeNames.Color3f).Set(
                Gf.Vec3f(*emissive)
            )

    # -- bind --------------------------------------------------------------- #
    material.CreateSurfaceOutput().ConnectToSource(
        surface.CreateOutput("surface", Sdf.ValueTypeNames.Token)
    )

    if recipe.get("note"):
        notes.append(recipe["note"])
    conditional = recipe.get("note_if_authored")
    if conditional and read_input(shader, _t(conditional[0])):
        notes.append(conditional[1])
    return notes


# --------------------------------------------------------------------------- #
# forcing both engines onto the preview surface
# --------------------------------------------------------------------------- #


def find_mdl_shader(material: UsdShade.Material):
    """The MDL shader prim, whether or not outputs:mdl:surface is still connected.

    ComputeSurfaceSource("mdl") is no use here: USD falls back to the universal
    context when the requested one has no source, so once we have blocked the MDL
    output it happily returns our own PreviewSurface. Follow the connection while
    there is one, and otherwise recognise the shader by its MDL source asset.
    """
    out = material.GetSurfaceOutput("mdl")
    if out and out.HasConnectedSource():
        source = out.GetConnectedSource()
        if source:
            return UsdShade.Shader(source[0].GetPrim())

    for child in material.GetPrim().GetChildren():
        shader = UsdShade.Shader(child)
        if not shader:
            continue
        asset = child.GetAttribute("info:mdl:sourceAsset")
        if asset and asset.HasAuthoredValue():
            return shader
    return None


def block_mdl(material: UsdShade.Material) -> bool:
    """Block outputs:mdl:surface so the mdl context resolves to our preview surface.

    RTX exposes no setting to choose a render context -- it is MDL-native and binds
    outputs:mdl:surface whenever it is connected. But UsdShadeMaterial falls back to
    the universal context when the requested one has no connected source, so cutting
    the one connection is enough to move Isaac onto the same UsdPreviewSurface that
    Unreal reads. The MDL shader prim and every one of its inputs stay in the layer
    untouched, so this is reversible with --restore-mdl and loses no authoring.

    Both engines then consume an identical material description, which is the point:
    the approximations this script makes become common-mode and cancel out of an
    Isaac-vs-Unreal comparison, instead of showing up as a difference we authored.
    """
    out = material.GetSurfaceOutput("mdl")
    if not out or not out.HasConnectedSource():
        return False
    out.DisconnectSource()
    return True


def restore_mdl(material: UsdShade.Material) -> bool:
    """Reconnect outputs:mdl:surface, putting Isaac back on the original MDL."""
    out = material.GetSurfaceOutput("mdl")
    if out and out.HasConnectedSource():
        return False
    shader = find_mdl_shader(material)
    if not shader:
        return False
    if not out:
        out = material.CreateSurfaceOutput("mdl")
    out.ConnectToSource(shader.CreateOutput("out", Sdf.ValueTypeNames.Token))
    return True


# --------------------------------------------------------------------------- #
# driving it over the tree
# --------------------------------------------------------------------------- #


def usd_files(root: str):
    """Every USD layer under `root`, in a stable order."""
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            if name.lower().endswith(USD_SUFFIXES):
                found.append(os.path.join(dirpath, name))
    return sorted(found)


def owned_materials(stage, root_layer):
    """Materials this layer actually authors.

    Opening a prop layer composes its references too, and writing an override for
    someone else's material here would both duplicate work and put the network in
    the wrong file.
    """
    for prim in stage.TraverseAll():
        if not prim.IsA(UsdShade.Material):
            continue
        stack = prim.GetPrimStack()
        if not stack or stack[0].layer.identifier != root_layer.identifier:
            continue
        yield UsdShade.Material(prim)


def process_layer(
    path: str, force: bool, keep_mdl: bool, dry_run: bool, stats: Counter, notes: Counter
) -> bool:
    """Bake preview surfaces, then block MDL, for materials this layer owns.

    The save happens here rather than in the caller because an Sdf.Layer handle
    expires as soon as the stage holding it goes out of scope -- returning the
    layer to save later hands back a dead object.
    """
    stage = Usd.Stage.Open(path, load=Usd.Stage.LoadNone)
    if not stage:
        stats["layer_unreadable"] += 1
        return False
    root_layer = stage.GetRootLayer()
    changed = False

    for material in owned_materials(stage, root_layer):
        shader = find_mdl_shader(material)

        # Bake, unless this material already carries a preview network. The skip is
        # deliberately not a `continue`: a layer baked by an earlier run still needs
        # the blocking pass below.
        surface = material.GetSurfaceOutput()
        if surface and surface.HasConnectedSource() and not force:
            stats["already_had_preview"] += 1
        elif not shader:
            stats["no_mdl_source"] += 1
            continue
        else:
            asset = shader.GetPrim().GetAttribute("info:mdl:sourceAsset")
            mdl_asset = asset.Get().path if asset and asset.Get() else ""
            module = posixpath.basename(mdl_asset.replace("\\", "/"))

            recipe = recipe_for(module)
            if recipe is None:
                stats["unknown_module"] += 1
                notes["unhandled module: " + (module or "<none>")] += 1
                continue

            for note in build_preview(stage, material, shader, recipe, mdl_asset):
                notes[note] += 1
            stats["converted"] += 1
            changed = True

        if not keep_mdl and block_mdl(material):
            stats["mdl_blocked"] += 1
            changed = True

    if changed and not dry_run:
        root_layer.Save()
    return changed


def restore_layer(path: str, dry_run: bool, stats: Counter) -> bool:
    """Reconnect outputs:mdl:surface for materials this layer owns."""
    stage = Usd.Stage.Open(path, load=Usd.Stage.LoadNone)
    if not stage:
        stats["layer_unreadable"] += 1
        return False
    root_layer = stage.GetRootLayer()
    changed = False

    for material in owned_materials(stage, root_layer):
        if restore_mdl(material):
            stats["mdl_restored"] += 1
            changed = True

    if changed and not dry_run:
        root_layer.Save()
    return changed


def update_manifest(manifest_path: str, source_root: str, touched):
    """Add the layers we rewrote to `locally_modified`, the hand-edit allowlist."""
    if not os.path.isfile(manifest_path):
        return
    with open(manifest_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)

    existing = list(manifest.get("locally_modified", []))
    known = set(existing)
    for path in touched:
        rel = os.path.relpath(path, source_root).replace("\\", "/")
        if rel not in known:
            existing.append(rel)
            known.add(rel)

    manifest["locally_modified"] = sorted(existing)
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")


def main() -> int:
    default_root = os.path.join(repo_root(), "data", "warehouse_source")

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--root", default=default_root, help="tree to scan (default: data/warehouse_source)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="report what would change, write nothing"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="rewrite the preview network even where outputs:surface already exists",
    )
    parser.add_argument(
        "--keep-mdl",
        action="store_true",
        help="bake only; leave outputs:mdl:surface connected so Isaac still renders MDL",
    )
    parser.add_argument(
        "--restore-mdl",
        action="store_true",
        help="undo the block: reconnect outputs:mdl:surface and leave the preview networks alone",
    )
    args = parser.parse_args()

    if not os.path.isdir(args.root):
        print(f"{PREFIX} no such tree: {args.root}")
        print(f"{PREFIX} run isaac\\scene\\fetch_warehouse_source.py first")
        return 1

    layers = usd_files(args.root)
    print(f"{PREFIX} scanning {len(layers)} USD layers under {args.root}")

    stats: Counter = Counter()
    notes: Counter = Counter()
    touched = []

    for path in layers:
        if args.restore_mdl:
            done = restore_layer(path, args.dry_run, stats)
        else:
            done = process_layer(path, args.force, args.keep_mdl, args.dry_run, stats, notes)
        if done:
            touched.append(path)

    print()
    if args.restore_mdl:
        print(f"{PREFIX} outputs:mdl:surface restored: {stats['mdl_restored']}")
    else:
        print(f"{PREFIX} materials converted        : {stats['converted']}")
        print(f"{PREFIX} already had outputs:surface: {stats['already_had_preview']}")
        print(f"{PREFIX} no mdl surface to read     : {stats['no_mdl_source']}")
        print(f"{PREFIX} unknown MDL module         : {stats['unknown_module']}")
        if not args.keep_mdl:
            print(f"{PREFIX} outputs:mdl:surface blocked: {stats['mdl_blocked']}")
    verb = "layers to rewrite" if args.dry_run else "layers rewritten"
    print(f"{PREFIX} {verb:<27}: {len(touched)}")

    if notes:
        print()
        print(f"{PREFIX} approximations and gaps (count x reason):")
        for note, count in notes.most_common():
            print(f"{PREFIX}   {count:5d}  {note}")

    if touched and not args.dry_run:
        update_manifest(os.path.join(args.root, "_fetch_manifest.json"), args.root, touched)
        print()
        print(f"{PREFIX} recorded {len(touched)} layers in _fetch_manifest.json locally_modified")

    if args.dry_run:
        print()
        print(f"{PREFIX} dry run -- nothing written")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
