// Copyright Epic Games, Inc. All Rights Reserved.

#pragma once

#include "USDGeomCameraTranslator.h"

#if USE_USD_SDK

class USceneComponent;

/**
 * Overrides the stock camera translator to carry UsdGeomCamera's physical exposure model
 * across to Unreal.
 *
 * UsdToUnreal::ConvertGeomCamera (USDPrimConversion.cpp:569) reads only the plain
 * `exposure` attribute, as AutoExposureBias. The four terms that actually set the
 * exposure -- exposure:time, exposure:iso, exposure:fStop and exposure:responsivity --
 * are dropped, and Unreal falls back to FPostProcessSettings defaults (1/60 s, ISO 100,
 * Scene.cpp:487-488) that have no relation to the stage. On this scene that alone is a
 * 6.43-stop divergence from Isaac Sim, which is the entire measured Gate 2 gap.
 *
 * The two engines implement the same model:
 *
 *   USD    UsdGeomCamera::ComputeLinearExposureScale, pxr/usd/usdGeom/camera.h:698
 *          scale = responsivity * time * (iso/100) * 2^exposure / fStop^2
 *
 *   Unreal GetPhysicalCameraEV100, PostProcessEyeAdaptation.cpp:395, where
 *          LuminanceMax = kISOSaturationSpeedConstant / LensAttenuation = 0.78/0.78 = 1
 *          scale = 2^AutoExposureBias / (DepthOfFieldFstop^2 * CameraShutterSpeed * 100/CameraISO)
 *
 * Feeding shutter, ISO and bias from USD makes them cancel exactly rather than
 * approximately. fStop already arrives through the stock path
 * (fStop -> CurrentAperture -> DepthOfFieldFstop, set by UCineCameraComponent::
 * UpdateCameraLens), so it is deliberately left alone here.
 *
 * Like the stock translator, every read is gated on HasAuthoredValue(): a camera prim
 * that says nothing about its exposure is left entirely to Unreal's own defaults.
 */
class FRenderGapGeomCameraTranslator : public FUsdGeomCameraTranslator
{
	using Super = FUsdGeomCameraTranslator;

public:
	using FUsdGeomCameraTranslator::FUsdGeomCameraTranslator;

	virtual void UpdateComponents(USceneComponent* SceneComponent) override;
};

#endif	  // #if USE_USD_SDK
