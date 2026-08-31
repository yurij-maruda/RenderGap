// Copyright Epic Games, Inc. All Rights Reserved.

using UnrealBuildTool;
using UnrealBuildTool.Rules;

public class RenderGapUSD : ModuleRules
{
	public RenderGapUSD(ReadOnlyTargetRules Target) : base(Target)
	{
		PCHUsage = PCHUsageMode.UseExplicitOrSharedPCHs;

		// The USD schema translator headers pull in the USD SDK/boost headers
		bUseRTTI = true;

		PublicDependencyModuleNames.AddRange(new string[]
		{
			"Core",
			"CoreUObject",
			"Engine",
		});

		PrivateDependencyModuleNames.AddRange(new string[]
		{
			"UnrealUSDWrapper", // USD SDK include paths, USE_USD_SDK and IMPLEMENT_MODULE_USD
			"USDClasses",
			"USDSchemas",       // FUsdLuxLightTranslator, which we derive from
			"USDUtilities",     // FUsdSchemaTranslatorRegistry, to register our translator
			"CinematicCamera",  // ACineCameraActor / UCineCameraComponent. USDSchemas links
			                    // these too, but only privately, so we need our own.
		});

		UnrealUSDWrapper.CheckAndSetupUsdSdk(Target, this);
	}
}
