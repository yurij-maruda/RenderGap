// Copyright Epic Games, Inc. All Rights Reserved.

#pragma once

#include "USDLuxLightTranslator.h"

#if USE_USD_SDK

class USceneComponent;

/**
 * Overrides the stock light translator to match Isaac Sim's rect light response:
 * a fixed attenuation radius, and an intensity that doesn't scale with the light's area.
 */
class FRenderGapLuxLightTranslator : public FUsdLuxLightTranslator
{
	using Super = FUsdLuxLightTranslator;

public:
	using FUsdLuxLightTranslator::FUsdLuxLightTranslator;

	/** Fixed attenuation radius (cm) assigned to every rect light. */
	static constexpr float AttenuationRadius = 4001.0f;

	/**
	 * Divisor applied to the lumen figure the USD importer produces, to match Isaac.
	 * Empirically fitted against a rendered frame -- see the .cpp for the derivation
	 * and for why it is not yet traceable to a line of engine source.
	 */
	static constexpr float IntensityDivisor = 2.0f;

	virtual void UpdateComponents(USceneComponent* SceneComponent) override;
};

#endif	  // #if USE_USD_SDK
