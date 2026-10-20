# SOCRadar Alarms for Microsoft Sentinel (no Logic App)

Microsoft Sentinel pulls your SOCRadar alarms itself. Nothing runs in your Azure subscription:
no Logic App, no Function App, no storage account. What you pay for is the data written to Log
Analytics.

[![Deploy to Azure](https://aka.ms/deploytoazurebutton)](https://portal.azure.com/#create/Microsoft.Template/uri/https%3A%2F%2Fraw.githubusercontent.com%2Forcunsami%2FSOCRadar-Azure-Incidents-CCF%2Fmaster%2Fazuredeploy.json)

## What it deploys

- A Microsoft Sentinel data connector (`SOCRadar Alarms`) that calls the SOCRadar incidents API every 5 minutes
- A data collection endpoint and rule, and the `SOCRadar_Alarms_CL` table
- 10 analytic rules, one per alarm category (including `Other`). Each raises one incident per OPEN alarm, titled `[SOCRadar] #<alarm id> - <title>`, labelled `SOCRadar`
- A workbook (`SOCRadar Alarms`)
- A workspace with Microsoft Sentinel enabled, if you ask for one

## Parameters

| Parameter | Description |
|-----------|-------------|
| `WorkspaceName` | Microsoft Sentinel workspace name (not the GUID) |
| `SocradarApiKey` | Your SOCRadar API key. A wrong key fails the deployment (401 on the connection check). Measured at deploy time only; a key revoked later, or a wrong company ID, was not tested |
| `CompanyId` | Your SOCRadar company ID |
| `DeployNewWorkspace` | `true` creates the workspace and enables Microsoft Sentinel on it. `false` uses an existing workspace that already has Microsoft Sentinel, in the same resource group. Default `true` |
| `WorkspaceLocation` | Region of the workspace. Defaults to the resource group region. Set it when the workspace is in another region |
| `TableRetentionDays` | Retention of `SOCRadar_Alarms_CL`. Default 90 |

## Check that it works

Give it 15 minutes after the first alarm, then run in the workspace:

```
SOCRadar_Alarms_CL | take 1
```

The first rows of a new table can take 6-15 minutes to show up. The connector page shows `Connected` for up to 30 days after the last data, so the table is the proof, not the page.

## Things to know

- Alarms flow in from the moment you deploy. Alarms raised before that are not imported, and an alarm
  SOCRadar writes late with an older date can be missed.
- The poller asks for alarms by creation date. An alarm whose status changes later (resolved, false positive) is not fetched again.
  The incident stays as it was opened.
- Seen live on the connector the Deploy button created: an alarm raised after the deploy (08:07 UTC) was in the table at 08:14
  and had an incident at 08:22, so about 7 minutes to the row and 15 minutes to the incident. This is one alarm, not a series.
  Reading existing alarms also worked on a connector re-created with a fixed start date (16 alarms, 16 incidents).
- Each alarm is stored with `AlarmText` cut to 4,000 characters, and the same cut text sits again inside `AlarmPayload` (checked on stored rows).
  Measured on a 329-alarm window of Executive Protection alarms: 9.8 KB per alarm after the cut (428 KB before it). Other categories were not measured.
  At that size 1,000 alarms is about 10 MB, roughly $0.08 at pay-as-you-go prices. Cost is the Log Analytics and Microsoft Sentinel ingestion only.
- Use a workspace name you have not used before. Re-using the name of a workspace deleted minutes ago made
  the analytic rule fail with "Log Analytics workspace could not be found".
- Alarms from before the deploy can be loaded once: post them to the data collection endpoint with the Logs Ingestion API (an Entra identity with the Monitoring Metrics Publisher role on the rule). Measured: 22 alarms of 30 days came in as 22 rows and 21 incidents. Sending the same alarms again added rows, not incidents.
- SOCRadar is only read. Closing an incident in Microsoft Sentinel does not change the alarm.
- An alarm that comes back after its incident was closed, or more than 7 days later, opens a new incident.
- A rule that is off for more than 10 minutes misses the alarms of that gap.
- Each rule raises at most 149 alerts per run. In a burst of more than 149 new OPEN alarms in one category within 10 minutes
  the rest never get an incident (measured: 329 alarms, 149 incidents).
- `Other` collects every type that has no rule of its own (in company 132's history that is most of Surface Web, Botnet, Threat Hunting and Phishing), so those share one 149 limit.
- Measured (20 Oct, UTC) on a connector re-created with a fixed start date and pages of 5: 23 alarms came in as 23 rows on each of three polls (69 rows, so paging worked) and as 22 incidents (the one RESOLVED alarm opened none), all labelled `SOCRadar`, no repeated title. The workbook rendered all five parts on those rows (status, main type, severity, per day, latest alarms), seen in the Azure Monitor view.
- If you ran an earlier version, delete its single rule `SOCRadar alarm` first, or alarms open two incidents.
- If you run the `SOCRadar-Alarm-Import` Logic App, turn it off before you deploy this, or every alarm opens two incidents.
  Both write to `SOCRadar_Alarms_CL`; deploy only one of them per workspace.
- SOCRadar allows 5 calls per second and answers a 429 (`rate limit exceeded 5 per 1 second`) with `retry-after` and `x-ratelimit-*` headers (measured). The connector sends pages at most once per second and is set to wait for the reset header on a 429. Under a parallel load on the same key (29% of the load's own calls got a 429 with the setting, 26% without it), polls still brought all 23 alarms: 2 polls with the setting, 1 without. The connector's own calls were not shown to hit a 429, so the setting is a safeguard, not a measured fix.
- The connection is created by the template. To stop polling, use the Delete button on the connector page (seen, not pressed) or the CLI (measured: the table and the rules stay, and no new row arrived in the 7 minutes after):
  `az rest --method GET --uri "https://management.azure.com/subscriptions/<sub>/resourceGroups/<rg>/providers/Microsoft.OperationalInsights/workspaces/<workspace>/providers/Microsoft.SecurityInsights/dataConnectors?api-version=2025-06-01" --query "value[].name"`, then the same URL with `/<name>` and `--method DELETE`.
- To rotate the API key, deploy again with the new key, `DeployNewWorkspace` set to `false`, and `WorkspaceLocation` if the workspace is not in the resource group's region.

## Push

A push connector, where SOCRadar sends the alarms, is in development and not available yet.

## Test

```
python3 tests/test_template.py
python3 tests/mutate_template.py
```
