"""Smoke test - simulatore canale testo (Task 15.1).

Centralino AI Studio Medico / Idelia (Flusso A).

Verifica che il simulatore canale testo (API Gateway `/simulate` -> Lambda
`motore-conversazionale`) RISPONDA senza toccare risorse a pagamento
(Requirement 1.6, 10.1). Lo smoke test copre due livelli, entrambi a COSTO ZERO
(nessuna rete, nessun deploy, nessun Bedrock reale):

  A) Runtime (contratto testo-in/testo-out): con Bedrock MOCKATO
     (PAID_COMPONENTS_ENABLED off) il thin handler costruisce il client mock e il
     Motore produce una risposta testuale. Si esercita anche la forma proxy HTTP
     API (`/simulate`) tramite parse_request/build_response, cosi' il "canale
     testo" e' verificato end-to-end senza numero telefonico reale.

  B) IaC (cablaggio CDK): il template sintetizzato espone una route POST /simulate
     su un'HTTP API integrata con la Lambda motore-conversazionale, e la Lambda
     ha PAID_COMPONENTS_ENABLED=false di default (Bedrock mockato -> costo zero).
     Nessuna risorsa a pagamento (Connect/Lex/Bedrock) e' sintetizzata.

Costo zero: nessun valore di segreto, nessun dato reale, nessuna chiamata AWS.
Commenti in ASCII.

Riferimento: design.md - "Simulatore canale testo (Requirement 1.6, 10.1)",
"Smoke test"; tasks.md Task 15 / 15.1.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
from typing import Any

import aws_cdk as cdk
from aws_cdk import assertions

from src.models import Session
from src.motore import MotoreConversazionale, ToolName
from stacks.centralino_stack import CentralinoStack


# ---------------------------------------------------------------------------
# Helper: import del thin handler (fuori dal package src)
# ---------------------------------------------------------------------------

def _import_handler_module():
    """Importa lambdas/motore_conversazionale.py via path (fuori da src/)."""
    root = pathlib.Path(__file__).resolve().parents[2]
    module_path = root / "lambdas" / "motore_conversazionale.py"
    spec = importlib.util.spec_from_file_location(
        "lambda_motore_conversazionale_smoke", module_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _SessionOnlyRepo:
    """Repository in-memory della sola SESSION (nessun backend, nessun costo)."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def get_session(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def save_session(self, session: Session) -> None:
        self._sessions[session.session_id] = session


class _NoopExecutor:
    """ToolExecutor fake: nessun tool viene invocato in questo smoke."""

    def execute(self, tool: ToolName, tool_input: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}


# ===========================================================================
# A) Runtime: il simulatore risponde a costo zero (Bedrock mockato)
# ===========================================================================

def test_simulatore_risponde_con_bedrock_mock(monkeypatch) -> None:
    """Con PAID_COMPONENTS_ENABLED off, il turno usa il client mock e risponde."""
    handler_mod = _import_handler_module()
    # Costo zero: nessun componente a pagamento -> client Bedrock mock.
    monkeypatch.delenv(handler_mod.ENV_PAID_COMPONENTS, raising=False)

    client = handler_mod._build_bedrock_client()
    # Il mock non chiama Bedrock: nessuna risorsa a pagamento toccata.
    assert type(client).__name__ == "_MockBedrockClient"

    motore = MotoreConversazionale(
        client=client,
        tool_executor=_NoopExecutor(),
        repository=_SessionOnlyRepo(),
        clock=lambda: 0.0,
    )

    result = motore.handle_turn("smoke-session", "Buongiorno, vorrei un appuntamento")

    # Il simulatore RISPONDE (testo non vuoto) senza fallimenti e senza tool.
    assert isinstance(result.reply, str) and result.reply.strip()
    assert result.tool_failed is False
    assert result.tool_invoked is None


def test_simulate_proxy_http_contratto_testo_in_testo_out(monkeypatch) -> None:
    """Forma proxy HTTP API /simulate: body JSON in -> statusCode 200 + body JSON.

    Esercita parse_request/build_response (le funzioni pure che il canale testo
    usa) con un evento in stile API Gateway v2, senza rete ne' Lambda reale.
    """
    handler_mod = _import_handler_module()
    monkeypatch.delenv(handler_mod.ENV_PAID_COMPONENTS, raising=False)

    # Evento proxy HTTP API v2 tipico (payload JSON nel body).
    event = {
        "version": "2.0",
        "routeKey": "POST /simulate",
        "requestContext": {"http": {"method": "POST", "path": "/simulate"}},
        "body": json.dumps({"sessionId": "sim-1", "text": "ciao"}),
        "isBase64Encoded": False,
    }

    session_id, user_text, channel = handler_mod.parse_request(event)
    assert session_id == "sim-1"
    assert user_text == "ciao"
    assert channel == "http"

    # Turno a costo zero con il client mock e repo in-memory.
    motore = MotoreConversazionale(
        client=handler_mod._build_bedrock_client(),
        tool_executor=_NoopExecutor(),
        repository=_SessionOnlyRepo(),
        clock=lambda: 0.0,
    )
    result = motore.handle_turn(session_id, user_text)

    # Risposta in forma proxy HTTP (statusCode/body) col contratto testo-out.
    response = handler_mod.build_response(session_id, result, channel)
    assert response["statusCode"] == 200
    payload = json.loads(response["body"])
    assert payload["sessionId"] == "sim-1"
    assert isinstance(payload["reply"], str) and payload["reply"].strip()
    assert payload["toolFailed"] is False


def test_parse_request_direct_invocation() -> None:
    """Invocazione diretta (test): campi al top level, canale 'direct'."""
    handler_mod = _import_handler_module()
    event = {"sessionId": "direct-1", "text": "pronto"}
    session_id, user_text, channel = handler_mod.parse_request(event)
    assert (session_id, user_text, channel) == ("direct-1", "pronto", "direct")


def test_parse_request_lex_v2_event() -> None:
    """Evento Lex V2 (strato voce): testo da inputTranscript, canale 'lex'."""
    handler_mod = _import_handler_module()
    event = {
        "sessionId": "lex-abc",
        "inputTranscript": "vorrei un appuntamento",
        "bot": {"name": "centralino-voce-idelia"},
        "sessionState": {"sessionId": "lex-abc", "intent": {"name": "CatchAllFallbackIntent"}},
        "invocationSource": "FulfillmentCodeHook",
    }
    session_id, user_text, channel = handler_mod.parse_request(event)
    assert channel == "lex"
    assert session_id == "lex-abc"
    assert user_text == "vorrei un appuntamento"


def test_build_response_lex_format() -> None:
    """La risposta per Lex ha sessionState (Close/Fulfilled) + messages col reply."""
    handler_mod = _import_handler_module()

    # Turno NON conclusivo (nessun tool o tool diverso da assegna_colloquio):
    # la sessione Lex deve restare aperta -> ElicitIntent (multi-turno).
    class _RContinua:
        reply = "Certo, mi dice il suo nome?"
        tool_invoked = None
        tool_failed = False

    resp = handler_mod.build_response("lex-abc", _RContinua(), "lex", "Conversazione")
    assert resp["sessionState"]["dialogAction"]["type"] == "ElicitIntent"
    assert resp["messages"][0]["content"] == "Certo, mi dice il suo nome?"
    assert resp["messages"][0]["contentType"] == "PlainText"

    # Turno CONCLUSIVO (assegna_colloquio riuscito): la sessione si chiude -> Close.
    class _ToolAssegna:
        value = "assegna_colloquio"

    class _RFine:
        reply = "Le ho fissato il colloquio. Arrivederci."
        tool_invoked = _ToolAssegna()
        tool_failed = False

    resp2 = handler_mod.build_response("lex-abc", _RFine(), "lex", "Conversazione")
    assert resp2["sessionState"]["dialogAction"]["type"] == "Close"
    assert resp2["sessionState"]["intent"]["state"] == "Fulfilled"
    assert resp2["sessionState"]["intent"]["name"] == "Conversazione"


# ===========================================================================
# B) IaC: /simulate cablato alla Lambda, Bedrock gated off (costo zero)
# ===========================================================================

def _synth_template(paid: bool = False):
    """Sintetizza lo stack e restituisce (template, stack)."""
    app = cdk.App()
    stack = CentralinoStack(
        app,
        "SmokeCentralinoStack",
        environment_name="dev",
        paid_components_enabled=paid,
        env=cdk.Environment(region="us-east-1"),
    )
    return assertions.Template.from_stack(stack), stack


def test_simulate_route_wired_to_motore_lambda() -> None:
    """Esiste una route POST /simulate su HTTP API integrata con la Lambda."""
    template, _ = _synth_template()
    # Una Lambda motore-conversazionale.
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"Handler": "lambdas.motore_conversazionale.handler"},
    )
    # Route POST /simulate sull'HTTP API.
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Route",
        {"RouteKey": "POST /simulate"},
    )
    # Le HTTP API dello stack sono due: il simulatore (/simulate) e il webhook
    # Telegram (/telegram/webhook, Task 11). La presenza della route /simulate
    # qui sopra garantisce che il simulatore sia cablato; il conteggio riflette
    # entrambe le API a costo zero (HTTP API rientra nel Free Tier).
    template.resource_count_is("AWS::ApiGatewayV2::Api", 2)
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Integration",
        {"IntegrationType": "AWS_PROXY"},
    )


def test_motore_lambda_bedrock_gated_off_by_default() -> None:
    """La Lambda ha PAID_COMPONENTS_ENABLED=false di default (Bedrock mockato)."""
    template, _ = _synth_template(paid=False)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": assertions.Match.object_like(
                    {"PAID_COMPONENTS_ENABLED": "false"}
                )
            }
        },
    )


def test_no_paid_resources_in_simulator_stack() -> None:
    """Nessuna risorsa a pagamento (Connect/Lex/Bedrock) nel template."""
    template, _ = _synth_template(paid=False)
    types = {
        res.get("Type")
        for res in template.to_json().get("Resources", {}).values()
    }
    paid_types = {
        "AWS::Connect::Instance",
        "AWS::Connect::ContactFlow",
        "AWS::Lex::Bot",
        "AWS::Bedrock::Agent",
    }
    assert not (types & paid_types), f"Risorse a pagamento inattese: {types & paid_types}"
