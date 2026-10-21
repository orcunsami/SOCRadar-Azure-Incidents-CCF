#!/usr/bin/env python3
"""Mutates a copy of azuredeploy.json one property at a time; test_template.py must fail on each.

The repo file is never written: every mutation goes to a temp file the test reads through
TEMPLATE_PATH. A run that crashes instead of reporting a failed check does not count as caught.

Run:  python3 tests/mutate_template.py
"""

import copy
import json
import os
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(REPO, "azuredeploy.json")
TEST = os.path.join(REPO, "tests", "test_template.py")
P = "Microsoft.OperationalInsights/workspaces"

with open(PATH, encoding="utf-8") as fh:
    BASE = json.load(fh)


def res(t, kind):
    return next(r for r in t["resources"] if r["type"].endswith(kind))


def conn(t):
    return res(t, "/dataConnectors")["properties"]


def dcr(t):
    return res(t, "dataCollectionRules")["properties"]


def rule(t):
    return res(t, "/alertRules")["properties"]


def swap(target, key, old, new):
    assert old in target[key], f"mutation anchor missing: {old!r}"
    target[key] = target[key].replace(old, new)


MUTATIONS = {
    # the poller
    "auth header renamed": lambda t: conn(t)["auth"].__setitem__("ApiKeyName", "Authorization"),
    "key prefix 'token ' comes back": lambda t: conn(t)["auth"].__setitem__("ApiKeyIdentifier", "token"),
    "key hard-coded": lambda t: conn(t)["auth"].__setitem__("ApiKey", "abc"),
    "end_date token dropped": lambda t: conn(t)["request"]["queryParameters"].pop("end_date"),
    "start_date token dropped": lambda t: conn(t)["request"]["queryParameters"].pop("start_date"),
    "time format changed": lambda t: conn(t)["request"].__setitem__("queryTimeFormat", "ISO 8601 UTC"),
    "paging removed": lambda t: conn(t).pop("paging"),
    "page parameter renamed": lambda t: conn(t)["paging"].__setitem__("pageNumberParaName", "p"),
    "events path changed": lambda t: conn(t)["response"].__setitem__("eventsJsonPaths", ["$.alarms[*]"]),
    "user agent dropped": lambda t: conn(t)["request"]["headers"].pop("User-Agent"),
    "POST instead of GET": lambda t: conn(t)["request"].__setitem__("httpMethod", "POST"),
    "company id cut from the endpoint": lambda t: swap(conn(t)["request"], "apiEndpoint", "parameters('CompanyId')", "'1001'"),
    "retryCount changed": lambda t: conn(t)["request"].__setitem__("retryCount", 9),
    "timeout changed": lambda t: conn(t)["request"].__setitem__("timeoutInSeconds", 5),
    "pages no longer throttled": lambda t: conn(t)["request"].pop("paginatedCallsPerSecond"),
    "pages throttled at another rate": lambda t: conn(t)["request"].__setitem__("paginatedCallsPerSecond", 5),
    "rate limit config removed": lambda t: conn(t)["request"].pop("rateLimitConfig"),
    "rate limit not evaluated on 429": lambda t: conn(t)["request"]["rateLimitConfig"]["evaluation"].__setitem__("checkMode", "Always"),
    "rate limit reset header renamed": lambda t: conn(t)["request"]["rateLimitConfig"]["extraction"]["headers"]["reset"].__setitem__("name", "retry-after"),
    "rate limit reset read as integer": lambda t: conn(t)["request"]["rateLimitConfig"]["extraction"]["headers"]["reset"].__setitem__("format", "Integer"),
    "429 retry ignores the reset header": lambda t: conn(t)["request"]["rateLimitConfig"]["retryStrategy"].__setitem__("useResetOrRetryAfterHeaders", False),
    "connector no longer waits for the DCR": lambda t: res(t, "/dataConnectors").__setitem__(
        "dependsOn", [d for d in res(t, "/dataConnectors")["dependsOn"] if "dataCollectionRules" not in d]),
    "connector writes to another stream": lambda t: conn(t)["dcrConfig"].__setitem__("streamName", "Custom-Other_CL"),
    # nothing may run per poll
    "Logic App added": lambda t: t["resources"].append({"type": "Microsoft.Logic/workflows", "name": "x"}),
    "storage account added": lambda t: t["resources"].append({"type": "Microsoft.Storage/storageAccounts", "name": "x"}),
    # DCE, DCR, table
    "DCE gets the Logic App product's name": lambda t: t["variables"].__setitem__("dceName", "SOCRadar-DCE"),
    "company id cut from the rows": lambda t: swap(dcr(t)["dataFlows"][0], "transformKql", "parameters('CompanyId')", "'1001'"),
    "transform stops projecting CompanyId": lambda t: swap(dcr(t)["dataFlows"][0], "transformKql", "CompanyId, AlarmMainType", "AlarmMainType"),
    "table column removed": lambda t: res(t, "/tables")["properties"]["schema"]["columns"].pop(2),
    "table column renamed": lambda t: res(t, "/tables")["properties"]["schema"]["columns"][1].__setitem__("name", "AlarmID"),
    "stream column dropped": lambda t: dcr(t)["streamDeclarations"]["Custom-SOCRadar_Alarms_CL"]["columns"].pop(3),
    "date keyword unquoted": lambda t: swap(dcr(t)["dataFlows"][0], "transformKql", '["date"]', "date"),
    "DCR no longer waits for the table": lambda t: res(t, "dataCollectionRules").__setitem__(
        "dependsOn", [d for d in res(t, "dataCollectionRules")["dependsOn"] if "tables" not in d]),
    "table no longer waits for onboarding": lambda t: res(t, "/tables").__setitem__("dependsOn", []),
    "DCR bound to nothing": lambda t: dcr(t).__setitem__("dataCollectionEndpointId", ""),
    # workspace
    "onboarding always created": lambda t: res(t, "onboardingStates").pop("condition"),
    "workspace condition inverted": lambda t: res(t, "workspaces").__setitem__("condition", "[not(parameters('DeployNewWorkspace'))]")
    if False else next(r for r in t["resources"] if r["type"] == P).__setitem__(
        "condition", "[not(parameters('DeployNewWorkspace'))]"),
    "location hard-coded": lambda t: res(t, "dataCollectionRules").__setitem__("location", "westeurope"),
    "location ignores the parameter": lambda t: t["variables"].__setitem__("location", "[resourceGroup().location]"),
    "WorkspaceLocation default changed": lambda t: t["parameters"]["WorkspaceLocation"].__setitem__("defaultValue", "northeurope"),
    # the rule
    "rule loses OPEN filter": lambda t: swap(rule(t), "query", 'toupper(Status) == "OPEN"', "true"),
    "OPEN filter before the latest row": lambda t: rule(t).__setitem__("query", rule(t)["query"].replace(
        '| summarize arg_max(TimeGenerated, *) by AlarmId\n| where toupper(Status) == "OPEN"\n',
        '| where toupper(Status) == "OPEN"\n| summarize arg_max(TimeGenerated, *) by AlarmId\n')),
    "rule loses grouping": lambda t: rule(t)["incidentConfiguration"]["groupingConfiguration"].__setitem__("enabled", False),
    "rule stops creating incidents": lambda t: rule(t)["incidentConfiguration"].__setitem__("createIncident", False),
    "rule runs hourly": lambda t: rule(t).__setitem__("queryFrequency", "PT1H"),
    "rule drops the severity column": lambda t: rule(t)["alertDetailsOverride"].pop("alertSeverityColumnName"),
    "rule loses the URL entity": lambda t: rule(t).__setitem__("entityMappings", []),
    "rule loses the take limit": lambda t: swap(rule(t), "query", "| take 149", ""),
    "rule no longer skips alerted alarms": lambda t: swap(rule(t), "query", "| where AlarmId !in (alerted)\n", ""),
    "alarm id regex loses its trailing space": lambda t: swap(rule(t), "query", '#(\\d+) "', '#(\\d+)"'),
    "queryPeriod back to 30 minutes": lambda t: rule(t).__setitem__("queryPeriod", "PT30M"),
    "ingestion window back to 10 minutes": lambda t: swap(rule(t), "query", "ingestion_time() > ago(1h)", "ingestion_time() > ago(10m)"),
    "alert name prefix loses its space": lambda t: swap(rule(t), "query", 'startswith "[SOCRadar] #"', 'startswith "[SOCRadar]#"'),
    "alerted list keeps alert names": lambda t: swap(rule(t), "query", "| distinct AlertAlarmId;", "| distinct AlertName;"),
    "alerted filter always true": lambda t: swap(rule(t), "query", "!in (alerted)", "!in (alerted) or true"),
    "queryFrequency back to PT5M": lambda t: rule(t).__setitem__("queryFrequency", "PT5M"),
    "rule id seed is the Logic App product's": lambda t: swap(res(t, "/alertRules"), "name", "socradar-ccf-alarm-rule", "socradar-alert-backed-rule"),
    "rule id ignores the main type": lambda t: swap(res(t, "/alertRules"), "name", ", variables('ruleTypes')[copyIndex()]))]", "))]"),
    "rule count typed in": lambda t: res(t, "/alertRules")["copy"].__setitem__("count", 10),
    "rules not looped": lambda t: res(t, "/alertRules").pop("copy"),
    "Other rule drops the exclusion": lambda t: swap(rule(t), "query", "concat('AlarmMainType !in ', variables('knownTypesKql'))", "'true'"),
    "typed rule filters on the wrong field": lambda t: swap(rule(t), "query", "AlarmMainType == ", "AlarmSubType == "),
    "Other no longer built from the list": lambda t: t["variables"].__setitem__("knownTypesKql", '("Fraud Protection")'),
    "ruleTypes loses Other": lambda t: t["variables"].__setitem__("ruleTypes", "[variables('mainTypes')]"),
    "mainTypes repeats a name": lambda t: t["variables"]["mainTypes"].append(t["variables"]["mainTypes"][0]),
    "rule carries AlarmText into the alert": lambda t: swap(rule(t), "query", "| project TimeGenerated, AlarmId, CompanyId, Title, AlarmDescription,",
                                                            "| project TimeGenerated, AlarmId, CompanyId, Title, AlarmText, AlarmDescription,"),
    "rule display name loses the type": lambda t: rule(t).__setitem__("displayName", "SOCRadar alarm"),
    # transform
    "AlarmText no longer cut": lambda t: swap(dcr(t)["dataFlows"][0], "transformKql", "AlarmText = substring(alarm_text, 0, 4000)", "AlarmText = alarm_text"),
    "alarm_text kept in the payload": lambda t: swap(dcr(t)["dataFlows"][0], "transformKql", " | project-away alarm_text", ""),
    "bag_remove_keys used": lambda t: swap(dcr(t)["dataFlows"][0], "transformKql", "AlarmPayload = pack_all()", "AlarmPayload = bag_remove_keys(pack_all(), dynamic([\"alarm_text\"]))"),
    # definition
    "IsConnectedQuery dropped": lambda t: res(t, "dataConnectorDefinitions")["properties"]["connectorUiConfig"].__setitem__(
        "connectivityCriteria", [{"type": "HasDataConnectors"}]),
    "HasDataConnectors dropped": lambda t: res(t, "dataConnectorDefinitions")["properties"]["connectorUiConfig"].__setitem__(
        "connectivityCriteria", [c for c in res(t, "dataConnectorDefinitions")["properties"]["connectorUiConfig"]["connectivityCriteria"] if c["type"] != "HasDataConnectors"]),
    # workbook
    "workbook removed": lambda t: t["resources"].remove(res(t, "/workbooks")),
    "workbook points at a typed-in workspace": lambda t: res(t, "/workbooks")["properties"].__setitem__("sourceId", "/subscriptions/x/resourceGroups/y/providers/Microsoft.OperationalInsights/workspaces/z"),
    "workbook content typed in": lambda t: res(t, "/workbooks")["properties"].__setitem__("serializedData", "{}"),
    "workbook reads another table": lambda t: t["variables"]["workbookContent"]["items"][1]["content"].__setitem__("query", "Other_CL | take 1"),
    "workbook old apiVersion": lambda t: res(t, "/workbooks").__setitem__("apiVersion", "2018-06-17-preview"),
    "automation rule removed": lambda t: t["resources"].remove(res(t, "/automationRules")),
    "automation rule points elsewhere": lambda t: res(t, "/automationRules")["properties"]["triggeringLogic"]["conditions"][0][
        "conditionProperties"].__setitem__("propertyValues", ["x"]),
    "automation rule no longer waits for the rules": lambda t: res(t, "/automationRules").__setitem__("dependsOn", []),
    "automation rule matches on a rule id": lambda t: res(t, "/automationRules")["properties"]["triggeringLogic"]["conditions"][0][
        "conditionProperties"].__setitem__("propertyName", "IncidentRelatedAnalyticRuleIds"),
    # parameters
    "API key becomes a plain string": lambda t: t["parameters"]["SocradarApiKey"].__setitem__("type", "string"),
    "empty API key accepted": lambda t: t["parameters"]["SocradarApiKey"].pop("minLength"),
    "empty company ID accepted": lambda t: t["parameters"]["CompanyId"].pop("minLength"),
    "PageSize parameter returns": lambda t: t["parameters"].__setitem__("PageSize", {"type": "int"}),
}

survivors = []
with tempfile.TemporaryDirectory() as tmp:
    target = os.path.join(tmp, "azuredeploy.json")
    for name, fn in MUTATIONS.items():
        mutated = copy.deepcopy(BASE)
        fn(mutated)
        with open(target, "w", encoding="utf-8") as fh:
            json.dump(mutated, fh, indent=2)
        run = subprocess.run([sys.executable, "-I", TEST], capture_output=True, text=True,
                             env={**os.environ, "TEMPLATE_PATH": target})
        caught = run.returncode != 0 and "TEMPLATE CHECK FAILED" in run.stdout
        print(f"  {'caught  ' if caught else 'SURVIVED'}  {name}")
        if not caught:
            survivors.append(name)

if survivors:
    print(f"\n{len(survivors)} mutation(s) survived: {survivors}")
    sys.exit(1)
print(f"\nAll {len(MUTATIONS)} mutations caught.")
