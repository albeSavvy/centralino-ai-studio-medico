"""Test integrazione/snapshot Step Functions confirmation-workflow (Task 11 / 11.1).

Centralino AI Studio Medico / Idelia (Flusso A) - design.md Decisione 4.

Copre (Requirements 6.3, 6.4, 6.5, 7.6, 7.7):

- CDK ASSERTIONS sulla state machine sintetizzata: presenza, tipo STANDARD,
  logging su CloudWatch Logs (LogLevel ALL), tracing X-Ray, tag obbligatori,
  Lambda-task cablate, grant SendTaskSuccess least-privilege alla Lambda
  telegram-callback e grant StartExecution alla Lambda motore.

- SNAPSHOT della definizione ASL: struttura degli stati e wiring chiave
  (waitForTaskToken col timeout esplicito di 8h - NON HeartbeatSeconds -,
  Catch States.Timeout -> ConfermatoAuto, Choice conferma/blocker, Retry 3 x >=2s
  sulla scrittura calendario, Catch -> CalendarioFallito).

- ESECUZIONE LOCALE (mock task) dei TRE esiti tramite un mini-interprete ASL che
  cammina gli stati parsati:
    * conferma -> ScritturaCalendario -> InviaConsenso -> Completato
    * blocker  -> DaRivedere -> DaRivedereFine (rescheduling out of scope)
    * timeout  -> ConfermatoAuto -> ScritturaCalendario -> ... -> Completato
  Il mini-interprete NON esegue le Lambda reali: simula gli esiti dei Task
  (callback action / timeout / successo calendario) e verifica che la macchina
  raggiunga lo stato terminale atteso. Nessuna risorsa AWS, nessun costo.

Tutti i test lavorano offline sul template sintetizzato / sulla definizione ASL:
nessun deploy, nessun bootstrap, nessuna esecuzione reale (guardrail costo-zero).

Commenti in ASCII.
"""
import json

import aws_cdk as cdk
from aws_cdk import assertions

from stacks.centralino_stack import CentralinoStack

SM_TYPE = "AWS::StepFunctions::StateMachine"


# ---------------------------------------------------------------------------
# Helper di sintesi + estrazione ASL
# ---------------------------------------------------------------------------

def _synth(paid: bool = False, environment_name: str = "dev"):
    """Sintetizza lo stack e restituisce (template, stack)."""
    app = cdk.App()
    stack = CentralinoStack(
        app,
        "TestSfnStack",
        environment_name=environment_name,
        paid_components_enabled=paid,
        env=cdk.Environment(region="us-east-1"),
    )
    return assertions.Template.from_stack(stack), stack


def _state_machine_resource(template):
    """Ritorna l'unica risorsa StateMachine del template."""
    machines = template.find_resources(SM_TYPE)
    assert len(machines) == 1, f"attesa 1 state machine, trovate {len(machines)}"
    return next(iter(machines.values()))


def _parse_asl(template):
    """Estrae e parsa la definizione ASL della state machine.

    DefinitionString embedda ARN/nomi delle Lambda (token CloudFormation): CDK la
    rappresenta come Fn::Join di parti. Sostituiamo ogni token con il placeholder
    "TOKEN" e poi facciamo il parse JSON, cosi' possiamo ispezionare la struttura
    logica degli stati (indipendente dagli ARN concreti risolti a deploy).
    """
    definition = _state_machine_resource(template)["Properties"]["DefinitionString"]
    if isinstance(definition, str):
        return json.loads(definition)
    if isinstance(definition, dict) and "Fn::Join" in definition:
        parts = definition["Fn::Join"][1]
        rebuilt = "".join(p if isinstance(p, str) else "TOKEN" for p in parts)
        return json.loads(rebuilt)
    raise AssertionError("formato DefinitionString non riconosciuto")


# Ordine canonico atteso degli stati (snapshot strutturale).
EXPECTED_STATES = {
    "Provvisorio",
    "AttendiConferma",
    "EsitoAzione",
    "ScritturaCalendario",
    "ConfermatoAuto",
    "InviaConsenso",
    "Completato",
    "CalendarioFallito",
    "CalendarioFallitoFine",
    "DaRivedere",
    "DaRivedereFine",
}


# ---------------------------------------------------------------------------
# CDK assertions: presenza, tipo, logging, tracing, tag
# ---------------------------------------------------------------------------

def test_state_machine_present_and_standard():
    """Esiste una sola state machine di tipo STANDARD (attese lunghe 8h)."""
    template, _ = _synth()
    template.resource_count_is(SM_TYPE, 1)
    template.has_resource_properties(
        SM_TYPE, {"StateMachineType": "STANDARD"}
    )


def test_state_machine_present_even_costzero_default():
    """La confirmation-workflow e' Free-Tier-friendly: esiste anche col flag off."""
    template, stack = _synth(paid=False)
    template.resource_count_is(SM_TYPE, 1)
    assert stack.confirmation_state_machine is not None


def test_state_machine_logging_enabled_all():
    """Logging su CloudWatch Logs con LogLevel ALL (osservabilita')."""
    template, _ = _synth()
    template.has_resource_properties(
        SM_TYPE,
        {
            "LoggingConfiguration": assertions.Match.object_like(
                {
                    "Level": "ALL",
                    "IncludeExecutionData": True,
                }
            )
        },
    )


def test_state_machine_tracing_enabled():
    """Tracing X-Ray abilitato."""
    template, _ = _synth()
    template.has_resource_properties(
        SM_TYPE,
        {"TracingConfiguration": assertions.Match.object_like({"Enabled": True})},
    )


def test_state_machine_log_group_present():
    """Esiste un log group dedicato alla state machine."""
    template, _ = _synth()
    log_groups = template.find_resources("AWS::Logs::LogGroup")
    names = [
        lg["Properties"].get("LogGroupName")
        for lg in log_groups.values()
    ]
    assert "/aws/vendedlogs/states/centralino-confirmation-workflow" in names


def test_state_machine_carries_mandatory_tags():
    """La state machine porta i 3 tag obbligatori (Requirement 9.5)."""
    template, _ = _synth()
    sm = _state_machine_resource(template)
    tags = {t["Key"]: t["Value"] for t in sm["Properties"].get("Tags", [])}
    assert tags.get("Project") == "centralino-ai-studio-medico"
    assert tags.get("Owner") == "savinoas"
    assert tags.get("Environment") == "dev"


def test_workflow_lambdas_present():
    """Le tre Lambda-task + la callback sono sintetizzate."""
    template, stack = _synth()
    fns = template.find_resources("AWS::Lambda::Function")
    names = {
        f["Properties"].get("FunctionName") for f in fns.values()
    }
    assert "centralino-notificatore-telegram" in names
    assert "centralino-scrivi-calendario" in names
    assert "centralino-gestore-consenso" in names
    assert "centralino-telegram-callback" in names
    # Attributi esposti per riuso.
    assert stack.notificatore_function is not None
    assert stack.scrivi_calendario_function is not None
    assert stack.gestore_consenso_function is not None
    assert stack.telegram_callback_function is not None


def test_callback_lambda_has_send_task_success_least_privilege():
    """telegram-callback puo' sbloccare SOLO questa state machine (no wildcard)."""
    template, _ = _synth()
    policies = template.find_resources("AWS::IAM::Policy")
    found = False
    for pol in policies.values():
        for stmt in pol["Properties"]["PolicyDocument"]["Statement"]:
            actions = stmt.get("Action")
            actions = [actions] if isinstance(actions, str) else (actions or [])
            if any("states:SendTaskSuccess" == a for a in actions):
                found = True
                # La risorsa non e' un wildcard "*": e' l'ARN della macchina (Ref/token).
                assert stmt.get("Resource") != "*", (
                    "SendTaskSuccess non deve usare Resource '*'"
                )
    assert found, "grant states:SendTaskSuccess non trovato per la Lambda callback"


def test_motore_can_start_execution():
    """La Lambda motore ha il grant StartExecution sulla state machine."""
    template, _ = _synth()
    policies = template.find_resources("AWS::IAM::Policy")
    found = any(
        any(
            "states:StartExecution"
            in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])
            for s in pol["Properties"]["PolicyDocument"]["Statement"]
            if s.get("Action")
        )
        for pol in policies.values()
    )
    assert found, "grant states:StartExecution non trovato per la Lambda motore"


def test_telegram_webhook_route_present():
    """Esiste la route POST /telegram/webhook verso la Lambda callback."""
    template, _ = _synth()
    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    keys = [r["Properties"].get("RouteKey") for r in routes.values()]
    assert "POST /telegram/webhook" in keys


# ---------------------------------------------------------------------------
# Snapshot strutturale della definizione ASL (Decisione 4)
# ---------------------------------------------------------------------------

def test_asl_start_and_states_snapshot():
    """Lo start e' Provvisorio e l'insieme degli stati e' quello atteso."""
    template, _ = _synth()
    asl = _parse_asl(template)
    assert asl["StartAt"] == "Provvisorio"
    assert set(asl["States"].keys()) == EXPECTED_STATES


def test_asl_wait_for_task_token_with_explicit_8h_timeout():
    """AttendiConferma usa waitForTaskToken con timeout ESPLICITO di 8h.

    Requirement 6.3/6.4, design.md Decisione 4: NON HeartbeatSeconds ma un
    TimeoutSeconds esplicito di 28800s (8h) sul task di callback.
    """
    template, _ = _synth()
    asl = _parse_asl(template)
    attendi = asl["States"]["AttendiConferma"]
    assert attendi["Type"] == "Task"
    assert attendi["Resource"].endswith("lambda:invoke.waitForTaskToken")
    # Timeout esplicito di 8 ore (8 * 3600 = 28800s).
    assert attendi["TimeoutSeconds"] == 8 * 3600
    # NON deve usare HeartbeatSeconds (design.md - Decisione 4).
    assert "HeartbeatSeconds" not in attendi
    # Il taskToken viaggia nel payload verso la Lambda notificatore.
    payload = attendi["Parameters"]["Payload"]
    assert payload.get("taskToken.$") == "$$.Task.Token"


def test_asl_timeout_routes_to_confermato_auto():
    """Il Catch States.Timeout di AttendiConferma instrada a ConfermatoAuto (6.4)."""
    template, _ = _synth()
    asl = _parse_asl(template)
    attendi = asl["States"]["AttendiConferma"]
    catches = attendi.get("Catch", [])
    timeout_catch = next(
        c for c in catches if "States.Timeout" in c["ErrorEquals"]
    )
    assert timeout_catch["Next"] == "ConfermatoAuto"
    # ConfermatoAuto annota CONFERMATO_AUTO e prosegue verso la scrittura.
    auto = asl["States"]["ConfermatoAuto"]
    assert auto["Result"]["status"] == "CONFERMATO_AUTO"
    assert auto["Next"] == "ScritturaCalendario"


def test_asl_choice_confirms_or_blocks():
    """EsitoAzione: blocker -> DaRivedere, altrimenti -> ScritturaCalendario (6.5)."""
    template, _ = _synth()
    asl = _parse_asl(template)
    choice = asl["States"]["EsitoAzione"]
    assert choice["Type"] == "Choice"
    blocker_branch = next(
        c for c in choice["Choices"] if c.get("StringEquals") == "blocker"
    )
    assert blocker_branch["Next"] == "DaRivedere"
    assert choice["Default"] == "ScritturaCalendario"
    # DaRivedere e' terminale (rescheduling out of scope).
    assert asl["States"]["DaRivedere"]["Next"] == "DaRivedereFine"
    assert asl["States"]["DaRivedereFine"]["Type"] == "Succeed"


def test_asl_calendar_retry_three_times_min_2s():
    """ScritturaCalendario: Retry max 3 con intervallo >= 2s (Requirement 7.6)."""
    template, _ = _synth()
    asl = _parse_asl(template)
    scrittura = asl["States"]["ScritturaCalendario"]
    retry_all = next(
        r for r in scrittura["Retry"] if "States.ALL" in r["ErrorEquals"]
    )
    assert retry_all["MaxAttempts"] == 3
    assert retry_all["IntervalSeconds"] >= 2
    assert retry_all["BackoffRate"] == 1  # intervallo costante >= 2s.


def test_asl_calendar_failure_routes_to_calendario_fallito():
    """Fallimento persistente -> CalendarioFallito -> Fail (Requirement 7.7)."""
    template, _ = _synth()
    asl = _parse_asl(template)
    scrittura = asl["States"]["ScritturaCalendario"]
    catch_all = next(
        c for c in scrittura["Catch"] if "States.ALL" in c["ErrorEquals"]
    )
    assert catch_all["Next"] == "CalendarioFallito"
    fallito = asl["States"]["CalendarioFallito"]
    assert fallito["Result"]["status"] == "SCRITTURA_CALENDARIO_FALLITA"
    assert fallito["Next"] == "CalendarioFallitoFine"
    assert asl["States"]["CalendarioFallitoFine"]["Type"] == "Fail"


def test_asl_success_path_reaches_completato():
    """Percorso di successo: ScritturaCalendario -> InviaConsenso -> Completato."""
    template, _ = _synth()
    asl = _parse_asl(template)
    assert asl["States"]["ScritturaCalendario"]["Next"] == "InviaConsenso"
    assert asl["States"]["InviaConsenso"]["Next"] == "Completato"
    assert asl["States"]["Completato"]["Type"] == "Succeed"


# ---------------------------------------------------------------------------
# Esecuzione locale (mock task): i tre esiti raggiungono lo stato atteso
# ---------------------------------------------------------------------------

# Task che nella realta' invocano una Lambda: nel mini-interprete ne simuliamo
# l'esito (successo con eventuale output) senza eseguire codice reale.
_TASK_STATES = {"AttendiConferma", "ScritturaCalendario", "InviaConsenso"}
# Stati terminali della macchina.
_TERMINAL_TYPES = {"Succeed", "Fail"}


def _run_asl(asl, task_outcomes):
    """Mini-interprete ASL: cammina gli stati e ritorna la lista degli stati visitati.

    Simula SOLO cio' che serve ai tre esiti (Task/Choice/Pass/Succeed/Fail):
      - Task: se task_outcomes[state] e' l'eccezione "TIMEOUT" segue il Catch
        States.Timeout; altrimenti prosegue su Next. Per AttendiConferma l'esito
        del callback (action) viene iniettato in context["callback"]["action"].
      - Choice: valuta la sola condizione StringEquals su $.callback.action che
        usiamo nel design; altrimenti Default.
      - Pass: applica Result (per annotare context) e prosegue su Next.
      - Succeed/Fail: terminano.

    Non e' un motore ASL completo: e' il minimo per verificare che i tre esiti
    del design (conferma / blocker / timeout) raggiungano lo stato terminale
    atteso attraversando gli stati previsti.

    Args:
        asl: definizione ASL parsata.
        task_outcomes: dict {nome_task: esito}. Per AttendiConferma l'esito e'
            "conferma" | "blocker" | "TIMEOUT". Per ScritturaCalendario e'
            "ok" | "FAIL". Assente = successo neutro.

    Returns:
        Lista ordinata dei nomi di stato visitati (incluso il terminale).
    """
    states = asl["States"]
    context = {"callback": {}}
    visited = []
    current = asl["StartAt"]

    while True:
        state = states[current]
        visited.append(current)
        stype = state["Type"]

        if stype in _TERMINAL_TYPES:
            return visited

        if stype == "Pass":
            current = state["Next"]
            continue

        if stype == "Choice":
            action = context["callback"].get("action")
            nxt = state["Default"]
            for choice in state["Choices"]:
                if (
                    choice.get("Variable") == "$.callback.action"
                    and choice.get("StringEquals") == action
                ):
                    nxt = choice["Next"]
                    break
            current = nxt
            continue

        if stype == "Task":
            outcome = task_outcomes.get(current)
            if outcome == "TIMEOUT":
                timeout_catch = next(
                    c for c in state.get("Catch", [])
                    if "States.Timeout" in c["ErrorEquals"]
                )
                current = timeout_catch["Next"]
                continue
            if outcome == "FAIL":
                catch_all = next(
                    c for c in state.get("Catch", [])
                    if "States.ALL" in c["ErrorEquals"]
                )
                current = catch_all["Next"]
                continue
            # AttendiConferma con callback: inietta l'action per il Choice.
            if current == "AttendiConferma" and outcome in ("conferma", "blocker"):
                context["callback"]["action"] = outcome
            current = state["Next"]
            continue

        raise AssertionError(f"tipo di stato non gestito nel mini-interprete: {stype}")


def _asl():
    template, _ = _synth()
    return _parse_asl(template)


def test_outcome_conferma_reaches_completato():
    """Esito CONFERMA: callback conferma -> calendario ok -> consenso -> Completato."""
    asl = _asl()
    visited = _run_asl(
        asl,
        task_outcomes={"AttendiConferma": "conferma", "ScritturaCalendario": "ok"},
    )
    assert visited[-1] == "Completato"
    assert "ScritturaCalendario" in visited
    assert "InviaConsenso" in visited
    # Non deve passare dal ramo blocker ne' dalla conferma automatica.
    assert "DaRivedere" not in visited
    assert "ConfermatoAuto" not in visited


def test_outcome_blocker_reaches_da_rivedere():
    """Esito BLOCKER: callback blocker -> DaRivedere -> DaRivedereFine (6.5)."""
    asl = _asl()
    visited = _run_asl(asl, task_outcomes={"AttendiConferma": "blocker"})
    assert visited[-1] == "DaRivedereFine"
    assert "DaRivedere" in visited
    # Il ramo blocker NON scrive calendario ne' invia consenso.
    assert "ScritturaCalendario" not in visited
    assert "InviaConsenso" not in visited


def test_outcome_timeout_reaches_confermato_auto_then_completato():
    """Esito TIMEOUT 8h: nessuna azione -> ConfermatoAuto -> ... -> Completato (6.4)."""
    asl = _asl()
    visited = _run_asl(
        asl,
        task_outcomes={"AttendiConferma": "TIMEOUT", "ScritturaCalendario": "ok"},
    )
    assert visited[-1] == "Completato"
    assert "ConfermatoAuto" in visited
    assert "ScritturaCalendario" in visited
    assert "InviaConsenso" in visited
    # La conferma automatica NON deve passare dal ramo blocker.
    assert "DaRivedere" not in visited


def test_outcome_confirm_but_calendar_fails_reaches_calendario_fallito():
    """Conferma ma scrittura calendario fallita dopo i retry -> CalendarioFallito (7.7)."""
    asl = _asl()
    visited = _run_asl(
        asl,
        task_outcomes={"AttendiConferma": "conferma", "ScritturaCalendario": "FAIL"},
    )
    assert visited[-1] == "CalendarioFallitoFine"
    assert "CalendarioFallito" in visited
    # Consenso non inviato se il calendario e' fallito.
    assert "InviaConsenso" not in visited
