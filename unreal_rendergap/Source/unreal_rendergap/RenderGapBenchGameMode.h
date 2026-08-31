// Copyright Epic Games, Inc. All Rights Reserved.

#pragma once

#include "CoreMinimal.h"
#include "GameFramework/GameModeBase.h"

#include "RenderGapBenchGameMode.generated.h"

/**
 * Points the player camera at the USD bench camera, so Movie Render Queue renders through
 * it in a headless -game run.
 *
 * Why not a Camera Cut track: the camera is spawned by the AUsdStageActor when it
 * translates the stage, so it is transient and its name is not stable across loads.
 * Sequencer possessables cannot reliably bind to that (UE-198531, and the same failure is
 * reported specifically for USD-imported levels), and authoring a UE 5.7 dynamic binding
 * requires a Director Blueprint endpoint that is awkward to generate from script.
 *
 * With no Camera Cut track MRQ falls back to the player's view target
 * (MoviePipeline.cpp:1403/1442/1720) and explicitly understands an ACineCameraActor there
 * (MoviePipelineBlueprintLibrary.cpp:993). So setting the view target is a supported path,
 * and it binds the actual stage-spawned camera rather than a stand-in.
 *
 * The stage is not translated by BeginPlay, so this retries every tick until the prim
 * resolves, then stops ticking. MRQ's EngineWarmUpCount covers the interval.
 */
UCLASS()
class ARenderGapBenchGameMode : public AGameModeBase
{
	GENERATED_BODY()

public:
	ARenderGapBenchGameMode();

	/** USD prim path of the camera to render through. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "RenderGap")
	FString CameraPrimPath = TEXT("/World/nova_carter/MainCamera");

	/** Give up (and log loudly) after this many seconds without the prim resolving. */
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category = "RenderGap")
	float ResolveTimeoutSeconds = 120.0f;

	virtual void StartPlay() override;
	virtual void Tick(float DeltaSeconds) override;

private:
	bool TryBindCamera();

	bool bBound = false;
	bool bGaveUp = false;
	float Elapsed = 0.0f;
};
