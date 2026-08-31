// Copyright Epic Games, Inc. All Rights Reserved.

#include "RenderGapGeomCameraTranslator.h"
#include "RenderGapLuxLightTranslator.h"

#include "Modules/ModuleManager.h"
#include "USDMemory.h"

#if USE_USD_SDK
#include "Objects/USDSchemaTranslator.h"
#endif	  // #if USE_USD_SDK

class FRenderGapUSDModule : public IModuleInterface
{
public:
	virtual void StartupModule() override
	{
#if USE_USD_SDK
		// The last translator registered for a schema is the one that gets used, so make sure
		// USDSchemas already registered the stock FUsdLuxLightTranslator before we register ours.
		FModuleManager::Get().LoadModule(TEXT("USDSchemas"));

		FUsdSchemaTranslatorRegistry& Registry = FUsdSchemaTranslatorRegistry::Get();

		// Same schema names the stock light translator uses: registering under a more specialized
		// name (UsdLuxRectLight) wouldn't work, as the registry matches the schemas in registration
		// order and would find UsdLuxBoundableLightBase first.
		TranslatorHandles = {
			Registry.Register<FRenderGapLuxLightTranslator>(TEXT("UsdLuxBoundableLightBase")),
			Registry.Register<FRenderGapLuxLightTranslator>(TEXT("UsdLuxNonboundableLightBase")),

			// The camera translator has no such subtlety: USDSchemasModule.cpp:46 registers the
			// stock FUsdGeomCameraTranslator under this exact name, so registering it again here
			// is enough to take over.
			Registry.Register<FRenderGapGeomCameraTranslator>(TEXT("UsdGeomCamera"))
		};
#endif	  // #if USE_USD_SDK
	}

	virtual void ShutdownModule() override
	{
#if USE_USD_SDK
		FUsdSchemaTranslatorRegistry& Registry = FUsdSchemaTranslatorRegistry::Get();

		for (const FRegisteredSchemaTranslatorHandle& TranslatorHandle : TranslatorHandles)
		{
			Registry.Unregister(TranslatorHandle);
		}
		TranslatorHandles.Empty();
#endif	  // #if USE_USD_SDK
	}

private:
#if USE_USD_SDK
	TArray<FRegisteredSchemaTranslatorHandle> TranslatorHandles;
#endif	  // #if USE_USD_SDK
};

IMPLEMENT_MODULE_USD(FRenderGapUSDModule, RenderGapUSD);
