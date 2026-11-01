#!/usr/bin/env python3
"""A wrong WorkspaceName must fail before anything is created.

With DeployNewWorkspace=false and a
WorkspaceName that does not exist, a template without this guard creates the data collection endpoint and rule
and only then fails. Depending on the workspace resource did not help:
ARM counts a resource whose `condition` is false as a satisfied dependency, so the
dependency graph protected nothing exactly where it looked like it did.

A `precheck-workspace-exists` nested deployment resolves the workspace with reference()
before anything else runs, and every other resource waits on it. Measured in Incidents, where
this was found on a live customer deploy: five resources became zero.

These checks pin the guard's shape, because a guard that cannot fail is not a guard:
  * inner expression evaluation -- with `outer`, reference() resolves in the parent scope
    and the guard succeeds whether the workspace exists or not
  * an actual reference() to the workspace -- without it the guard always succeeds
  * gated on the create toggle -- otherwise it looks for a workspace it is about to create
  * every other resource waits on it -- otherwise they are created before it fails
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.environ.get("TEMPLATE_PATH") or os.path.join(os.path.dirname(HERE), "azuredeploy.json")
GUARD = "precheck-workspace-exists"
GUARD2 = "precheck-external-workspace"
CROSS_RG = False
WS_TYPE = "Microsoft.OperationalInsights/workspaces"

failures = []
checks = 0


def check(condition, message):
    global checks
    checks += 1
    if not condition:
        failures.append(message)


with open(TEMPLATE) as handle:
    root = json.load(handle)

resources = [r for r in root.get("resources", []) if isinstance(r, dict)]
check(bool(resources), "azuredeploy.json: no resources - this check has gone blind")
check(any(r.get("type") == WS_TYPE for r in resources),
      "azuredeploy.json: no workspace resource - this check has gone blind")

# The default is false: the template does not update a workspace that already exists, so a
# commitment tier can never be touched; a missing name fails before anything is created.
# Creating a workspace is opt-in (true). The toggle sits right under WorkspaceName so the
# portal form shows it there.
params = root.get("parameters", {})
toggle = params.get("DeployNewWorkspace", {})
check(toggle.get("defaultValue") is False,
      "azuredeploy.json: DeployNewWorkspace must default to false")
check(list(params)[:2] == ["WorkspaceName", "DeployNewWorkspace"],
      "azuredeploy.json: DeployNewWorkspace must be declared directly after WorkspaceName, found %s" % list(params)[:2])

names = [GUARD] + ([GUARD2] if CROSS_RG else [])

for name in names:
    found = [r for r in resources if r.get("name") == name]
    check(len(found) == 1,
          "azuredeploy.json: expected exactly one %s, found %d" % (name, len(found)))
    if not found:
        continue
    guard = found[0]
    props = guard.get("properties", {})
    inner = props.get("template", {})

    check(props.get("expressionEvaluationOptions", {}).get("scope") == "inner",
          "%s: not inner scope, so its reference() resolves in the parent and never fails"
          % name)
    outputs = json.dumps(inner.get("outputs") or {})
    check("reference(" in outputs,
          "%s: does not reference anything, so it succeeds either way" % name)
    check(".name]" not in outputs,
          "%s: reads .name off a Full reference, which does not exist on these proxy "
          "resources - the guard would fail even in the healthy case" % name)
    check(bool(guard.get("condition")),
          "%s: unconditional" % name)

if any(r.get("name") == GUARD for r in resources):
    guard = [r for r in resources if r.get("name") == GUARD][0]
    # This template onboards Microsoft Sentinel only on a workspace it creates, so the guard has to
    # read both the workspace and its onboarding state; either one alone leaves a half install.
    outs = guard.get("properties", {}).get("template", {}).get("outputs", {})
    check("reference(parameters('WorkspaceResourceId')" in json.dumps(outs.get("workspaceId", {})),
          "%s: workspaceId does not reference the workspace itself" % GUARD)
    check("onboardingStates" in json.dumps(outs.get("sentinelOnboarded", {}))
          and "reference(" in json.dumps(outs.get("sentinelOnboarded", {})),
          "%s: does not assert that Microsoft Sentinel is enabled on the workspace" % GUARD)
    check("DeployNewWorkspace" in json.dumps(guard.get("condition")) or
          "deployWorkspace" in json.dumps(guard.get("condition")),
          "%s: not gated on the create toggle - it would look for a workspace this "
          "deployment is about to create" % GUARD)
    check("workspaceResourceId" in json.dumps(guard.get("properties", {})),
          "%s: does not resolve the workspace resource id" % GUARD)

if CROSS_RG and any(r.get("name") == GUARD2 for r in resources):
    guard2 = [r for r in resources if r.get("name") == GUARD2][0]
    check("isExternalWorkspace" in json.dumps(guard2.get("condition")),
          "%s: not gated on the workspace being external - it would demand onboarding in "
          "the same-RG case, where this template does the onboarding itself" % GUARD2)
    check("onboardingStates" in json.dumps(guard2.get("properties", {})),
          "%s: does not check Microsoft Sentinel onboarding, so a cross-RG deployment onto "
          "a workspace without Microsoft Sentinel still succeeds" % GUARD2)

# Nothing may run before the guards. A resource missing from this list is one that gets
# created before the deployment fails, which is the whole defect.
for name in names:
    dep = "[resourceId('Microsoft.Resources/deployments', '%s')]" % name
    unguarded = [r.get("name") for r in resources
                 if r.get("name") not in names
                 and r.get("type") != WS_TYPE
                 and dep not in (r.get("dependsOn") or [])]
    check(not unguarded,
          "azuredeploy.json: %d resource(s) do not wait for %s and would be created before "
          "it fails: %s" % (len(unguarded), name, unguarded[:4]))

# A guard that depends on itself is rejected at validation with "cannot reference itself",
# so the whole deployment never starts -- including the healthy case. The static shape looks
# fine, which is why this needs its own check: it was produced by an automated port and only
# a live deploy surfaced it.
for resource in resources:
    rname = resource.get("name")
    if not isinstance(rname, str):
        continue
    for dep in (resource.get("dependsOn") or []):
        check(rname not in dep,
              "azuredeploy.json: %s depends on itself (%s) - ARM rejects the template at "
              "validation and nothing deploys at all" % (rname, dep))

print("\n".join("  - %s" % f for f in failures))
if failures:
    print("%d problem(s) in %d checks." % (len(failures), checks))
    sys.exit(1)
print("workspace pre-check: guard present, can fail, and nothing runs before it "
      "(%d checks)" % checks)
