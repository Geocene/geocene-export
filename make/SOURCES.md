# Blueprint provenance

Research checked 28 October 2026. JSON parsing and mapping-reference checks do
not prove that a blueprint imports or executes in a Make account. This kit does
not claim account-level validation.

## Official Make contracts

- [Make API: Templates](https://developers.make.com/api-documentation/api-reference/templates)
  contains a full root `flow`/`metadata` blueprint example and a legacy
  `http:ActionGetFile` v3 module. This kit uses that envelope and download module
  contract. Module download output is passed to the destination as file data.
- [HTTP legacy documentation](https://apps.make.com/http-legacy) documents request
  methods, raw JSON bodies, parsed responses, timeouts, and authenticated
  requests. [Current HTTP documentation](https://apps.make.com/http) explains
  dedicated credential storage and Download a file. The kit deliberately uses
  legacy v3 serialization rather than guessing the new v4 blueprint format.
- [API-key authentication](https://apps.make.com/api-key-authentication-type)
  documents header placement, the parameter name, and inclusion of a required
  prefix such as `Token` in the stored key.
- [JSON](https://apps.make.com/json) documents Create JSON and its account-specific
  data structure. The kit leaves the data-structure ID unconfigured and includes
  a generator sample instead of distributing someone else's structure ID.
- [Data stores](https://help.make.com/l6du-data-stores) documents persistence
  across runs, Add/Replace a Record, and Search Records. The store is intentionally
  unbound; users select their own and recreate the small provided record schema.
- [Router](https://help.make.com/router) documents route filters and fallback.
- [Skip error handler](https://help.make.com/skip-error-handler) documents Skip, which
  allows subsequent bundles to continue after a handled error. The polling
  network modules use this handler; their job record and attempt were already
  persisted before the failing request.
- [Blueprints](https://help.make.com/blueprints) documents importing into an empty
  scenario and configuring the user's own connections afterward.
- [Templates](https://help.make.com/create-and-manage-scenario-templates)
  documents public sharing links and a guided setup wizard for a later,
  account-tested release.
- [Scheduling](https://help.make.com/schedule-a-scenario) documents on-demand and
  interval schedules; [pricing](https://www.make.com/en/pricing) is the current
  reference for plan allowances. No allowance is hardcoded into the kit.

## Primary exported examples used to check serialization

These are creators' actual published module/blueprint examples, not a formal
Make JSON Schema or evidence that this new scenario has been run. Only the
module contracts were consulted; customer data, connections, and unrelated
workflow content were not copied.

- [API-key request and router module export by samliew](https://community.make.com/t/skip-predetermined-holidays/83479/3):
  `http:ActionSendDataAPIKeyAuth` v3, `parameters.auth`,
  `keychain:apikeyauth`, `builtin:BasicRouter`, nested `routes[].flow`,
  `util:SetVariable2`, and filter layout.
- [Raw JSON API-key request export by samliew](https://community.make.com/t/how-to-compress-images-using-tinypng-via-api/36641/4):
  `bodyType=raw`, `contentType=application/json`, and `mapper.data`.
- [Published data-store blueprint example](https://gist.github.com/stillgrowingshop-rgb/de94755d6e160e76b33b814bc885e3fa):
  `datastore:SearchRecord` v1, `parameters.datastore`,
  `continueWhenNoRes`, `limit`, `mapper.filter`, `sort`, and output `key`/`data`.
- [Published AddRecord blueprint example](https://ohmydev.be/agent/piloter-la-satisfaction-client-avec-des-enquetes-automatisees/):
  `datastore:AddRecord` v1 with `mapper.key`, `data`, and `overwrite`.
- [Published Create JSON export](https://community.make.com/t/scenario-blueprint-json-string-text-parser-replace-regex/4169):
  `json:CreateJSON` v1 with `parameters.type`, `space`, and structured mapper.
- [Set multiple variables module export by samliew](https://community.make.com/t/iterating-over-an-array-an-setting-variables-conditionally/35227/2):
  `util:SetVariables` v1 with `mapper.variables`, `name`, `value`, and
  `scope=roundtrip`. The settings module uses native Boolean values for defaults.
- [Published blueprint with an Ignore handler](https://gist.github.com/BlackandBlue1908/be9846404a5064aba72cc650f6e6a651):
  the network module's `onerror` array contains a `builtin:Ignore` v1 module with
  an independent ID and designer metadata. The current UI calls this behavior
  Skip; the blueprint keeps the verified serialized identifier.

The Geocene endpoint names, filters, response paths, and status handling were
verified against the application's source and are summarized in [API.md](../API.md).
