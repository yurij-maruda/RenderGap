// Copyright Epic Games, Inc. All Rights Reserved.

using UnrealBuildTool;

public class unreal_rendergap : ModuleRules
{
	public unreal_rendergap(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;
	
		PublicDependencyModuleNames.AddRange(new string[] { "Core", "CoreUObject", "Engine", "InputCore", "EnhancedInput" });

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"CinematicCamera", // ACineCameraActor, the view target ARenderGapBenchGameMode binds
			"USDStage",        // AUsdStageActor::GetGeneratedComponent, to find it by prim path
		});

		// Uncomment if you are using Slate UI
		// PrivateDependencyModuleNames.AddRange(new string[] { "Slate", "SlateCore" });
		
		// Uncomment if you are using online features
		// PrivateDependencyModuleNames.Add("OnlineSubsystem");

		// To include OnlineSubsystemSteam, add it to the plugins section in your uproject file with the Enabled attribute set to true
	}
}
