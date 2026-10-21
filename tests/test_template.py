#!/usr/bin/env python3
"""Pins what makes this template cheap and correct.

Nothing in the customer's Azure may run per poll: no Logic App, no Function App. Microsoft
Sentinel polls SOCRadar itself (RestApiPoller). The checks read the template as structure, not
as text, and fail when one of those properties is lost.

Run:  python3 tests/test_template.py            (checks azuredeploy.json)
      TEMPLATE_PATH=/some/copy.json python3 tests/test_template.py
"""

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.environ.get("TEMPLATE_PATH") or os.path.join(REPO, "azuredeploy.json")

P = "Microsoft.OperationalInsights/workspaces"
ALLOWED_TYPES = {
    P,
    "Microsoft.OperationsManagement/solutions",
    P + "/providers/onboardingStates",
    P + "/tables",
    "Microsoft.Insights/dataCollectionEndpoints",
    "Microsoft.Insights/dataCollectionRules",
    P + "/providers/dataConnectorDefinitions",
    P + "/providers/dataConnectors",
    P + "/providers/alertRules",
    P + "/providers/automationRules",
    "Microsoft.Insights/workbooks",
}
STREAM = "Custom-SOCRadar_Alarms_CL"
# Raw SOCRadar fields the DCR transform reads. The stream has to declare every one of them.
RAW_FIELDS = ("alarm_id", "alarm_risk_level", "status", "alarm_text", "date", "alarm_type_details")
PROJECTED = {"TimeGenerated", "AlarmId", "CompanyId", "AlarmMainType", "AlarmSubType", "Severity", "Status", "Title",
             "AlarmText", "AlarmDate", "AlarmPayload", "AlarmUrl", "EventType"}
# Columns the push connector fills. The pull transform leaves all but AlarmUrl and EventType empty.
PUSH_COLS = {"EventType": "string", "CompanyName": "string", "AlarmAsset": "string", "AlarmUrl": "string",
             "Tags": "dynamic", "AlarmUpdatedAt": "string", "ConnectorVersion": "string"}
TEST_ROWS = '| where isnotempty(AlarmId)\n| where column_ifexists("EventType", "") != "connection.test"\n'
SENT_URL = ('| extend AlarmUrl = coalesce(column_ifexists("AlarmUrl", ""), '
            'strcat("https://platform.socradar.com/company/", CompanyId, "/alarm/", AlarmId))\n')

failures = []


def check(condition, message):
    if not condition:
        failures.append(message)


def one(by_type, kind):
    found = by_type.get(kind, [])
    check(len(found) == 1, f"expected exactly 1 {kind.split('/')[-1]}, found {len(found)}")
    return found[0] if found else None


def depends(resource, needle):
    return any(needle in d for d in resource.get("dependsOn", []))


with open(PATH, encoding="utf-8") as fh:
    raw = fh.read()
template = json.loads(raw)
resources = template["resources"]
variables = template.get("variables", {})
by_type = {}
for r in resources:
    by_type.setdefault(r["type"], []).append(r)

# ---- nothing runs, nothing is billed per poll ----
for r in resources:
    check(r["type"] in ALLOWED_TYPES, f"resource type not allowed: {r['type']}")
for banned in ("Microsoft.Logic", "Microsoft.Web", "Microsoft.Storage"):
    check(not any(t.startswith(banned) for t in by_type),
          f"{banned} resource found: something would run or be billed per poll")

# ---- the poller ----
conn = one(by_type, P + "/providers/dataConnectors")
if conn:
    p = conn["properties"]
    check(conn.get("kind") == "RestApiPoller", "data connector is not a RestApiPoller")
    check(conn["apiVersion"] == "2025-09-01", f"connector apiVersion changed: {conn['apiVersion']}")
    auth = p.get("auth", {})
    check(auth.get("type") == "APIKey", "auth type is not APIKey")
    check(auth.get("ApiKeyName") == "API-Key", "SOCRadar expects the header name API-Key")
    check(auth.get("ApiKeyIdentifier") == "", "ApiKeyIdentifier must be empty, or the key is sent as 'token <key>'")
    check(auth.get("ApiKey") == "[parameters('SocradarApiKey')]", "ApiKey must come from the secure parameter")
    req = p.get("request", {})
    check(req.get("httpMethod") == "GET", "SOCRadar is read with GET only")
    check(req.get("queryTimeFormat") == "UnixTimestamp", "SOCRadar start_date/end_date are epoch seconds")
    check(req.get("queryWindowInMin") == 5, "poll window changed")
    check(req.get("rateLimitQPS") == 1 and req.get("retryCount") == 3 and req.get("timeoutInSeconds") == 60,
          "rateLimitQPS / retryCount / timeoutInSeconds changed")
    # SOCRadar allows 5 calls/s (x-ratelimit-limit, measured 21 Oct). rateLimitQPS does not apply
    # to pages, so paginatedCallsPerSecond must be set too.
    check(req.get("paginatedCallsPerSecond") == 1, "pages are not throttled: paginatedCallsPerSecond != 1")
    rl = req.get("rateLimitConfig", {})
    hdrs = rl.get("extraction", {}).get("headers", {})
    check(rl.get("evaluation", {}).get("checkMode") == "OnlyWhen429", "rate limit is not evaluated on 429")
    check(rl.get("extraction", {}).get("source") == "CustomHeaders", "rate limit headers are not read")
    check(hdrs.get("limit", {}).get("name") == "x-ratelimit-limit"
          and hdrs.get("remaining", {}).get("name") == "x-ratelimit-remaining"
          and hdrs.get("reset", {}).get("name") == "x-ratelimit-reset", "rate limit header names changed")
    check(hdrs.get("reset", {}).get("format") == "UnixTimeSeconds", "x-ratelimit-reset is epoch seconds")
    check(rl.get("retryStrategy", {}).get("useResetOrRetryAfterHeaders") is True, "429 retry ignores the reset header")
    qp = req.get("queryParameters", {})
    check(qp.get("start_date") == "{_QueryWindowStartTime}", "start_date token missing")
    check(qp.get("end_date") == "{_QueryWindowEndTime}", "end_date token missing (windows would overlap)")
    check(qp.get("limit") == "100", "limit must match pageSize")
    endpoint = req.get("apiEndpoint", "")
    check("https://platform.socradar.com/api/company/" in endpoint and "parameters('CompanyId')" in endpoint
          and "/incidents/v4'" in endpoint, "endpoint is not built from the CompanyId parameter")
    check("User-Agent" in req.get("headers", {}), "no User-Agent on the SOCRadar call")
    paging = p.get("paging", {})
    check(paging.get("pagingType") == "CountBasedPaging" and paging.get("pageNumberParaName") == "page",
          "paging must be CountBasedPaging on 'page'")
    check(paging.get("pageSize") == 100 and not paging.get("zeroBasedIndexing"), "pageSize/indexing changed")
    check(p.get("response", {}).get("eventsJsonPaths") == ["$.data[*]"], "alarms are in $.data[*]")
    check(p.get("dataType") == "[variables('tableName')]" and variables.get("tableName") == "SOCRadar_Alarms_CL",
          "dataType must be the SOCRadar_Alarms_CL table")
    check(p.get("dcrConfig", {}).get("streamName") == STREAM, "connector writes to another stream")
    check(depends(conn, "dataCollectionRules") and depends(conn, "dataConnectorDefinitions"),
          "connector must depend on the DCR and the definition")

# ---- DCE / DCR / table ----
dce = one(by_type, "Microsoft.Insights/dataCollectionEndpoints")
dcr = one(by_type, "Microsoft.Insights/dataCollectionRules")
table = one(by_type, P + "/tables")
check(variables.get("dceName") == "SOCRadar-CCF-DCE",
      "DCE name must not be the one the Logic App product uses (same resource group would share it)")
if dcr and table:
    dp = dcr["properties"]
    flow = dp["dataFlows"][0]
    kql = flow["transformKql"]
    check(flow["outputStream"] == STREAM and flow["streams"] == [STREAM], "DCR stream changed")
    check("parameters('CompanyId')" in kql and "replace(" in kql, "CompanyId is not written into the rows")
    check('["date"]' in kql, "bare `date` is a KQL keyword and breaks the DCR")
    check("AlarmText = substring(alarm_text, 0, 4000)" in kql,
          "AlarmText must be cut: some alarms carry 300 KB of text and would be stored in full")
    check(kql.find("project-away alarm_text") != -1 and kql.find("project-away alarm_text") < kql.find("pack_all()"),
          "alarm_text must be dropped before pack_all(), or the long text is stored twice")
    check("bag_remove_keys" not in kql, "bag_remove_keys is rejected by the DCR transform compiler")
    declared = {c["name"] for c in dp["streamDeclarations"].get(STREAM, {}).get("columns", [])}
    for col in RAW_FIELDS:
        check(col in declared, f"stream does not declare {col}")
        check(col in kql, f"transformKql does not read {col}")
    table_cols = {c["name"]: c["type"] for c in table["properties"]["schema"]["columns"]}
    projected = re.search(r"\| project ([^|']+)'", kql)
    projected_cols = {c.strip() for c in projected.group(1).split(",")} if projected else set()
    check(projected_cols == PROJECTED, f"transform output changed: missing {sorted(PROJECTED - projected_cols)}, "
          f"extra {sorted(projected_cols - PROJECTED)}")
    check(projected_cols <= set(table_cols), f"transform writes columns the table lacks: {sorted(projected_cols - set(table_cols))}")
    # The push connector writes these too; same table, so both templates must declare them.
    for col, kind in PUSH_COLS.items():
        check(table_cols.get(col) == kind, f"table column {col} ({kind}) missing")
    check('| extend AlarmUrl = strcat("https://platform.socradar.com/company/", CompanyId, "/alarm/", AlarmId), '
          'EventType = "alarm.created" | project' in kql, "transform no longer sets AlarmUrl and EventType")
    check(table["properties"]["schema"]["name"] == "[variables('tableName')]", "table schema name is not the table variable")
    check(depends(dcr, "dataCollectionEndpoints") and depends(dcr, "tables"), "DCR must depend on the DCE and the table")
    check("dataCollectionEndpoints" in dp.get("dataCollectionEndpointId", ""), "DCR is not bound to the DCE")

# ---- workspace: created only when asked ----
create = "[parameters('DeployNewWorkspace')]"
ws = [r for r in resources if r["type"] == P]
sol = [r for r in resources if r["type"] == "Microsoft.OperationsManagement/solutions"]
onb = [r for r in resources if r["type"] == P + "/providers/onboardingStates"]
for label, group in (("workspace", ws), ("solution", sol), ("onboarding state", onb)):
    check(len(group) == 1 and group[0].get("condition") == create,
          f"{label} must exist only when DeployNewWorkspace is true")
if table:
    check(depends(table, "onboardingStates"), "table must depend on the onboarding state")
definition = one(by_type, P + "/providers/dataConnectorDefinitions")
if definition:
    check(depends(definition, "onboardingStates"), "definition must depend on the onboarding state")
    kinds = [c.get("type") for c in definition["properties"]["connectorUiConfig"]["connectivityCriteria"]]
    check("HasDataConnectors" in kinds and "IsConnectedQuery" in kinds, "connectivityCriteria must carry both HasDataConnectors and IsConnectedQuery")
    check(definition["properties"]["connectorUiConfig"]["id"] == "[variables('definitionName')]"
          and conn and conn["properties"]["connectorDefinitionName"] == "[variables('definitionName')]",
          "connector UI id and connectorDefinitionName differ")

# ---- the rules: one alert, one incident, per OPEN alarm, one rule per main type ----
rules = by_type.get(P + "/providers/alertRules", [])
auto = one(by_type, P + "/providers/automationRules")
check(len(rules) == 1 and rules[0].get("copy", {}).get("name") == "socradarAlarmRules",
      "the alarm rules must be one resource with copy name socradarAlarmRules")
main_types = variables.get("mainTypes", [])
check(len(main_types) >= 1 and len(set(main_types)) == len(main_types), "mainTypes must be a list of distinct names")
check(variables.get("ruleTypes") == "[concat(variables('mainTypes'), createArray('Other'))]",
      "ruleTypes must be mainTypes plus the Other catch-all, built from the one list")
check(variables.get("knownTypesKql") == "[replace(replace(string(variables('mainTypes')), '[', '('), ']', ')')]",
      "the Other filter must be built from mainTypes, not typed out again")
if rules:
    rule = rules[0]
    check(rule.get("copy", {}).get("count") == "[length(variables('ruleTypes'))]", "rule count must follow ruleTypes")
    check("socradar-ccf-alarm-rule" in rule["name"] and "variables('ruleTypes')[copyIndex()]" in rule["name"],
          "rule id must come from the type and the CCF seed, not the Logic App product's")
    rp = rule["properties"]
    q = rp["query"]
    check(rp["queryFrequency"] == "PT10M" and rp["queryPeriod"] == "PT1H", "rule schedule changed: expected PT10M / PT1H")
    # A run cannot see alerts of a run less than about 6 minutes earlier (Sentinel ends the query
    # window about 6 minutes before the run), so a shorter schedule raises every alarm twice.
    freq = re.fullmatch(r"PT(\d+)([MH])", rp["queryFrequency"])
    check(freq and int(freq.group(1)) * (60 if freq.group(2) == "H" else 1) >= 10,
          "queryFrequency under 10 minutes: a run would not see the previous run's alerts")
    check("ingestion_time() > ago(1h)" in q, "rule lost its ingestion window")
    # A burst over 149 is spread over later runs: each run skips alarms that already have an alert.
    alerted = ('let alerted = SecurityAlert\n| where TimeGenerated > ago(1h)\n'
               '| where AlertName startswith "[SOCRadar] #"\n'
               '| extend AlertAlarmId = tostring(extract(@"#(\\d+) ", 1, AlertName))\n'
               '| distinct AlertAlarmId;')
    check(alerted in q, "the alerted block changed: it must list the alarm ids of '[SOCRadar] #<id> ' alerts of the last hour")
    check("| where AlarmId !in (alerted)\n| where " in q, "rule no longer skips exactly the alarms that already have an alert")
    window = re.search(r"ingestion_time\(\) > ago\((\d+)([mh])\)", q)
    period = re.fullmatch(r"PT(\d+)([MH])", rp["queryPeriod"])
    to_min = lambda m: int(m.group(1)) * (60 if m.group(2).lower() == "h" else 1)
    check(window and period and to_min(period) >= to_min(window),
          "queryPeriod must cover the ingestion window, or older rows are cut")
    check("take 149" in q, "rule must stay under the 150 alerts a run limit")
    check(TEST_ROWS in q, "rule no longer skips SOCRadar's connection.test rows")
    check(SENT_URL in q, "rule must use the AlarmUrl SOCRadar sends and build the platform link only when none is sent")
    check("if(equals(variables('ruleTypes')[copyIndex()], 'Other'), concat('AlarmMainType !in ', variables('knownTypesKql'))" in q,
          "the Other rule must exclude every main type that has its own rule")
    check("concat('AlarmMainType == \"', variables('ruleTypes')[copyIndex()]" in q, "a typed rule must filter on its own main type")
    a_, o_ = q.find("summarize arg_max(TimeGenerated, *) by AlarmId"), q.find('toupper(Status) == \"OPEN\"')
    o_ = q.find('toupper(Status) == "OPEN"') if o_ == -1 else o_
    check(a_ != -1 and o_ != -1 and a_ < o_,
          "the latest row per alarm must be picked before the OPEN filter, or a closed alarm opens an incident")
    check("project TimeGenerated, AlarmId, CompanyId, Title, AlarmDescription," in q and "AlarmText," not in q.split("| project")[1],
          "the rule must not carry AlarmText into the alert")
    override = rp.get("alertDetailsOverride", {})
    check("SentinelSeverity" in q and override.get("alertSeverityColumnName") == "SentinelSeverity", "severity mapping lost")
    check(override.get("alertDisplayNameFormat") == "[SOCRadar] #{{AlarmId}} - {{Title}}", "incident title format changed")
    check("variables('ruleTypes')[copyIndex()]" in rp.get("displayName", ""), "rule display name must carry the main type")
    ic = rp["incidentConfiguration"]
    gc = ic["groupingConfiguration"]
    check(ic["createIncident"] is True, "rule no longer creates incidents")
    check(gc["enabled"] is True and gc["groupByEntities"] == ["URL"] and gc["lookbackDuration"] == "P7D"
          and gc["matchingMethod"] == "Selected", "alert grouping on the alarm URL is what stops duplicate incidents")
    check(any(m["entityType"] == "URL" and m["fieldMappings"][0]["columnName"] == "AlarmUrl"
              for m in rp.get("entityMappings", [])), "URL entity mapping lost")
    check(rp["eventGroupingSettings"]["aggregationKind"] == "AlertPerResult", "one alert per alarm")
if auto:
    cond = auto["properties"]["triggeringLogic"]["conditions"][0]["conditionProperties"]
    check(cond.get("propertyName") == "IncidentTitle" and cond.get("operator") == "StartsWith"
          and cond.get("propertyValues") == ["[[SOCRadar]"],
          "automation rule must label by the [SOCRadar] title prefix")
    check(auto["dependsOn"] == ["socradarAlarmRules"], "automation rule must wait for the whole rule loop")
    check(auto["properties"]["actions"][0]["actionConfiguration"]["labels"] == [{"labelName": "SOCRadar"}],
          "incident label changed")

# ---- the workbook ----
wb = one(by_type, "Microsoft.Insights/workbooks")
if wb:
    check(wb["apiVersion"] == "2021-08-01" and wb.get("kind") == "shared", "workbook apiVersion/kind changed")
    check(wb["name"] == "[variables('workbookId')]" and "guid(" in variables.get("workbookId", ""), "workbook name must be a guid")
    wp = wb["properties"]
    check(wp.get("serializedData") == "[string(variables('workbookContent'))]", "workbook content must come from the variable")
    check(wp.get("sourceId") == "[resourceId('Microsoft.OperationalInsights/workspaces', parameters('WorkspaceName'))]",
          "workbook must point at the workspace parameter, never a typed-in workspace")
    items = variables.get("workbookContent", {}).get("items", [])
    queries = [i["content"]["query"] for i in items if i.get("type") == 3]
    check(len(queries) >= 4 and all(qq.startswith("SOCRadar_Alarms_CL") for qq in queries),
          "every workbook query must read SOCRadar_Alarms_CL")

# ---- everywhere else ----
check(variables.get("location") == "[parameters('WorkspaceLocation')]", "location variable must follow the WorkspaceLocation parameter")
check(template["parameters"]["WorkspaceLocation"].get("defaultValue") == "[resourceGroup().location]", "WorkspaceLocation must default to the resource group region")
for r in resources:
    if "location" in r:
        check(r["location"] == "[variables('location')]", f"{r['type']}: location is not the variable")
check(template["parameters"]["SocradarApiKey"]["type"] == "securestring", "API key must be a securestring")
check(template["parameters"]["SocradarApiKey"].get("minLength") == 1, "an empty API key must be refused before the deployment starts")
check(template["parameters"]["CompanyId"].get("minLength") == 1, "an empty company ID must be refused before the deployment starts")
check("PageSize" not in template["parameters"], "PageSize parameter came back")
check(template["contentVersion"] == "1.0.0", "contentVersion must stay 1.0.0")
check(not re.search(r"[A-Fa-f0-9]{32,}", raw), "long hex string in the template")
check("Azure Sentinel" not in raw, "'Azure Sentinel' is rejected by Content Hub review; use Microsoft Sentinel")

if failures:
    print("TEMPLATE CHECK FAILED\n")
    for item in failures:
        print("  - " + item)
    print(f"\n{len(failures)} problem(s).")
    sys.exit(1)

print(f"Template: {len(resources)} resources, no Logic App / Function App / storage, "
      f"poller, paging, tokens, columns, dependencies and rule pinned.")
