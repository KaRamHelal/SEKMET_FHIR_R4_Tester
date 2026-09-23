"""Generated CapabilityStatement reflecting what this server actually supports."""
from __future__ import annotations

from .common import FHIR_VERSION, instant
from .search_params import PARAMS, params_for, supported_types

KIND_TYPE = {"string": "string", "token": "token", "reference": "reference", "date": "date", "number": "number",
             "uri": "uri"}
INTERACTIONS = ["read", "vread", "update", "patch", "delete", "history-instance", "history-type", "create",
                "search-type"]


def capability_statement(svc) -> dict:
    s = svc.settings
    resources = []
    for rtype in supported_types():
        params = [{"name": n, "type": KIND_TYPE[p.kind]} for n, p in params_for(rtype).items()]
        params.insert(0, {"name": "_id", "type": "token"})
        res = {
            "type": rtype,
            "versioning": "versioned-update",
            "readHistory": True,
            "updateCreate": True,
            "conditionalCreate": True,
            "conditionalRead": "not-supported",
            "conditionalUpdate": True,
            "conditionalDelete": "single",
            "referencePolicy": ["literal", "logical", "local"],
            "interaction": [{"code": c} for c in INTERACTIONS],
            "searchInclude": ["*", *[f"{rtype}:{n}" for n, p in PARAMS.get(rtype, {}).items() if p.kind == "reference"]],
            "searchRevInclude": [f"{t}:{n}" for t, ps in PARAMS.items() if t != "*"
                                 for n, p in ps.items() if p.kind == "reference" and (p.target in (None, rtype))][:200],
            "searchParam": params,
        }
        ops = [{"name": name.lstrip("$"), "definition": f"http://hl7.org/fhir/OperationDefinition/{rtype}-{name.lstrip('$')}"}
               for (t, name) in svc.operations if t == rtype]
        if rtype == "Subscription":
            from ..subscriptions.backport import EXT_TOPIC_CANONICAL, PROFILE
            res["supportedProfile"] = [PROFILE]
            engine = getattr(getattr(svc, "ctx_ref", None), "subscriptions", None)
            topics = engine.topics() if engine else {}
            res["extension"] = [{"url": EXT_TOPIC_CANONICAL, "valueCanonical": url} for url in sorted(topics)]
            for o in ops:
                if o["name"] == "status":
                    o["definition"] = "http://hl7.org/fhir/uv/subscriptions-backport/OperationDefinition/backport-subscription-status"
        if ops:
            res["operation"] = ops
        resources.append(res)
    security: dict = {"cors": True}
    types = s.server_auth.types
    if "smart" in types:
        security["service"] = [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/restful-security-service",
                                            "code": "SMART-on-FHIR"}]}]
        security["extension"] = [{
            "url": "http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris",
            "extension": [{"url": "token", "valueUri": f"{s.root_url}/auth/token"}],
        }]
    elif "basic" in types:
        security["service"] = [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/restful-security-service",
                                            "code": "Basic"}]}]
    security["description"] = f"Accepted inbound auth: {', '.join(types)}"
    return {
        "resourceType": "CapabilityStatement",
        "id": "sekmet",
        "url": f"{svc.base_url}/metadata",
        "name": "SekmetCapabilityStatement",
        "title": f"{s.software_name} CapabilityStatement",
        "status": "active",
        "date": instant(),
        "publisher": s.software_name,
        "kind": "instance",
        "software": {"name": s.software_name, "version": "0.1.0"},
        "implementation": {"description": f"{s.facility_name} (micro HIS test harness)", "url": svc.base_url},
        "fhirVersion": FHIR_VERSION,
        "format": ["application/fhir+json", "json"],
        "patchFormat": ["application/json-patch+json", "application/fhir+json"],
        "rest": [{
            "mode": "server",
            "security": security,
            "resource": resources,
            "interaction": [{"code": "transaction"}, {"code": "batch"}, {"code": "history-system"}],
            "operation": [{"name": n.lstrip("$"), "definition": f"http://hl7.org/fhir/OperationDefinition/{n.lstrip('$')}"}
                          for (t, n) in svc.operations if t == "*"],
        }],
        "messaging": [{
            "endpoint": [{"protocol": {"system": "http://terminology.hl7.org/CodeSystem/message-transport",
                                       "code": "http"},
                          "address": f"{svc.base_url}/$process-message"}],
            "documentation": "Accepts FHIR message Bundles at $process-message; see README for supported events",
        }],
    }
