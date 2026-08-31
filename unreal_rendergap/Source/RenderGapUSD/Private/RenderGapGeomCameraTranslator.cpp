// Copyright Epic Games, Inc. All Rights Reserved.

#include "RenderGapGeomCameraTranslator.h"

#if USE_USD_SDK

#include "USDConversionUtils.h"
#include "USDMemory.h"

#include "UsdWrappers/UsdPrim.h"

#include "CineCameraActor.h"
#include "CineCameraComponent.h"

#include "USDIncludesStart.h"
#include "pxr/usd/usd/attribute.h"
#include "pxr/usd/usd/prim.h"
#include "pxr/usd/usdGeom/camera.h"
#include "USDIncludesEnd.h"

void FRenderGapGeomCameraTranslator::UpdateComponents(USceneComponent* SceneComponent)
{
	// Run the stock ten-attribute conversion first; we only add the exposure terms it drops.
	Super::UpdateComponents(SceneComponent);

	if (!SceneComponent)
	{
		return;
	}

	// We may have been handed the spawned ACineCameraActor's root component rather than the
	// camera component itself -- same fallback FUsdGeomCameraTranslator::UpdateComponents uses.
	UCineCameraComponent* CameraComponent = Cast<UCineCameraComponent>(SceneComponent);
	if (!CameraComponent)
	{
		if (ACineCameraActor* CameraActor = Cast<ACineCameraActor>(SceneComponent->GetOwner()))
		{
			if (SceneComponent == CameraActor->GetRootComponent())
			{
				CameraComponent = CameraActor->GetCineCameraComponent();
			}
		}
	}

	if (!CameraComponent)
	{
		return;
	}

	FScopedUsdAllocs Allocs;

	pxr::UsdGeomCamera GeomCamera{pxr::UsdPrim{GetPrim()}};
	if (!GeomCamera)
	{
		return;
	}

	const pxr::UsdTimeCode TimeCode{Context->Time};

	CameraComponent->Modify();

	// ConvertGeomCamera assigns Filmback.SensorWidth/SensorHeight as plain fields
	// (USDPrimConversion.cpp:551-557) and never re-runs RecalcDerivedData afterwards -- the
	// only calls to it come from the SetCurrentFocalLength/SetCurrentAperture setters
	// earlier in the same function. So Filmback.SensorAspectRatio keeps whatever the
	// component was constructed with: 1.7778, from the "16:9 Digital Film" default preset,
	// even though the imported sensor is 4:3.
	//
	// That matters because UCameraComponent::AspectRatio derives from SensorAspectRatio
	// (CineCameraComponent.cpp:536) and ACineCameraActor ships with bConstrainAspectRatio
	// true -- so an 800x600 Movie Render Queue job would letterbox against a 16:9 camera.
	// Re-assigning the filmback through its setter recomputes it; the values are unchanged.
	CameraComponent->SetFilmback(CameraComponent->Filmback);

	// Depth of field off, unconditionally. Spec section 3.1 makes it a controlled variable.
	//
	// The camera has to carry a real focusDistance for Isaac's sake -- Kit's DOF reads the
	// prim's physical fStop/focusDistance, so a 0 there puts the focal plane at zero and
	// smears the whole frame in any viewport with DOF enabled. But a non-zero focusDistance
	// gives Unreal FocusMethod = Manual (USDPrimConversion.cpp:534), i.e. DOF on.
	//
	// Disable is the right method rather than DoNotOverride: it forces
	// DepthOfFieldFocalDistance = 0 (CineCameraComponent.cpp:722) instead of deferring to
	// whatever a post-process volume might say, and it still takes the branch that sets
	// DepthOfFieldFstop = CurrentAperture (:707), so the exposure aperture is preserved.
	CameraComponent->FocusSettings.FocusMethod = ECameraFocusMethod::Disable;

	FPostProcessSettings& Post = CameraComponent->PostProcessSettings;
	bool bAnyAuthored = false;

	// exposure:time is a duration in seconds; CameraShutterSpeed is its reciprocal (1/s).
	if (pxr::UsdAttribute Attr = GeomCamera.GetExposureTimeAttr(); Attr.HasAuthoredValue())
	{
		const float ExposureTime = UsdUtils::GetUsdValue<float>(Attr, TimeCode);
		if (ExposureTime > UE_SMALL_NUMBER)
		{
			Post.bOverride_CameraShutterSpeed = true;
			Post.CameraShutterSpeed = 1.0f / ExposureTime;
			bAnyAuthored = true;
		}
	}

	if (pxr::UsdAttribute Attr = GeomCamera.GetExposureIsoAttr(); Attr.HasAuthoredValue())
	{
		const float ExposureIso = UsdUtils::GetUsdValue<float>(Attr, TimeCode);
		if (ExposureIso > UE_SMALL_NUMBER)
		{
			Post.bOverride_CameraISO = true;
			Post.CameraISO = ExposureIso;
			bAnyAuthored = true;
		}
	}

	// USD folds `responsivity` into the same product as the rest; Unreal has no equivalent
	// term. 2^AutoExposureBias reproduces it, and `exposure` is already a stops offset that
	// the stock translator puts in that same field, so the two simply add.
	if (pxr::UsdAttribute Attr = GeomCamera.GetExposureResponsivityAttr(); Attr.HasAuthoredValue())
	{
		const float Responsivity = UsdUtils::GetUsdValue<float>(Attr, TimeCode);
		if (Responsivity > UE_SMALL_NUMBER)
		{
			float Exposure = 0.0f;
			if (pxr::UsdAttribute ExposureAttr = GeomCamera.GetExposureAttr(); ExposureAttr.HasAuthoredValue())
			{
				Exposure = UsdUtils::GetUsdValue<float>(ExposureAttr, TimeCode);
			}

			Post.bOverride_AutoExposureBias = true;
			Post.AutoExposureBias = Exposure + FMath::Log2(Responsivity);
			bAnyAuthored = true;
		}
	}

	// Only opt a camera in when the stage actually said something about its exposure. The
	// physical model is meaningless under histogram auto-exposure, so both of these have to
	// come along with it -- but forcing them onto every camera prim in the stage (Nova
	// Carter alone carries a dozen sensor cameras) would be a side effect nobody asked for.
	if (bAnyAuthored)
	{
		Post.bOverride_AutoExposureMethod = true;
		Post.AutoExposureMethod = AEM_Manual;

		Post.bOverride_AutoExposureApplyPhysicalCameraExposure = true;
		Post.AutoExposureApplyPhysicalCameraExposure = 1;
	}
}

#endif	  // #if USE_USD_SDK
