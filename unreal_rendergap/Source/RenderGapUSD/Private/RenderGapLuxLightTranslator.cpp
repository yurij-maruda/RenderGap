// Copyright Epic Games, Inc. All Rights Reserved.

#include "RenderGapLuxLightTranslator.h"

#if USE_USD_SDK

#include "Components/RectLightComponent.h"

void FRenderGapLuxLightTranslator::UpdateComponents(USceneComponent* SceneComponent)
{
	// Let the stock translator convert the prim first, then fix up the rect lights.
	// This runs again on every resync/time change, and since the base translator always
	// recomputes the intensity from USD, our division is not applied twice.
	Super::UpdateComponents(SceneComponent);
	
	URectLightComponent* RectLightComponent = Cast<URectLightComponent>(SceneComponent);
	if (!RectLightComponent)
	{
		return;
	}

	RectLightComponent->AttenuationRadius = AttenuationRadius;

	// EMPIRICAL, not derived. See docs/usd_transfer_losses.md section 1.
	//
	// The area-normalised model (divide by Area) that this used to implement predicts a
	// 1.00x match and measures 22x too dark. Working backwards from the rendered frame:
	//
	//   importer raw   I * PI * Area = 15000 * PI * 40 = 1.885e6 lumens
	//   required       1.885e6 / 22                    = 1.037e6 lumens
	//   => divisor     1.82,  i.e. ~2, NOT the 40 that Area gives
	//
	// The 22x was established by elimination, not assumption: global illumination accounts
	// for at most 1.32x of it (UE NoGI vs Path Tracer) and materials for at most 1.18x
	// (five material regions all landing between 20.9x and 24.6x), so what remains is a
	// near-uniform scale on the light itself.
	//
	// CAVEAT: both rect lights in this scene are 4x10 m, so a constant divisor of 2 and an
	// area-dependent Area/20 fit the data identically. A second light of a different size
	// is needed to tell them apart. Treated as a constant here because that is the simpler
	// of the two, not because it is established.
	RectLightComponent->Intensity /= IntensityDivisor;
	
	RectLightComponent->MarkRenderStateDirty();
}

#endif	  // #if USE_USD_SDK
