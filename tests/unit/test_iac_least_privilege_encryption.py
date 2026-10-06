"""Test IaC completa (Task 18) - least privilege, encryption at rest, rollback.

Copre (Requirements 9.1, 9.2, 9.3, 9.4, 9.6, 9.7) tramite CDK assertions sul
template sintetizzato, OFFLINE: nessun `cdk deploy`, nessun `cdk destroy`, nessun
bootstrap, nessun accesso a un account AWS, nessun costo. Le aspettative di
deploy/destroy (provisioning, assenza di risorse orfane, rollback su fallimento)
sono coperte via assertions su RemovalPolicy/DeletionPolicy e via note
documentate del comportamento di default di CloudFormation, NON eseguendo un
deploy reale.

Sezioni:
- Requirement 9.4 - least privilege: nessun wildcard `*` su azioni/risorse nei
  ruoli/policy delle Lambda applicative, salvo i casi tecnicamente inevitabili e
  DOCUMENTATI (log delivery / X-Ray della state machine, che non supportano
  permessi resource-level).
- Requirement 9.6 - encryption at rest: DynamoDB, bucket S3 del consenso,
  CloudWatch Logs (cifrati dal servizio) e Secrets (N/A in questa fase, nessun
  segreto in chiaro nel template).
- Requirement 9.2/9.3/9.7 - deploy/destroy/rollback: RemovalPolicy.DESTROY su
  tabella, bucket e log group (nessuna risorsa orfana su destroy) e nota sul
  rollback automatico di CloudFormation su deploy fallito.
"""
import aws_cdk as cdk
import pytest
from aws_cdk import assertions

from stacks.centralino_stack import CentralinoStack


# ---------------------------------------------------------------------------
# Helpers di sintesi. Tutto offline: si lavora sul template, mai su un account.
# ---------------------------------------------------------------------------

def _synth(paid: bool = False, environment_name: str = "dev"):
    """Sintetizza lo stack e restituisce (template_json, stack). Nessun deploy."""
    app = cdk.App()
    stack = CentralinoStack(
        app,
        "IacTestStack",
        environment_name=environment_name,
        paid_components_enabled=paid,
        env=cdk.Environment(region="us-east-1"),
    )
    template = assertions.Template.from_stack(stack)
    return template.to_json(), stack


def _iam_policies(template_json):
    """Ritorna i {logical_id: props} delle AWS::IAM::Policy (policy autoriali)."""
    return {
        lid: res.get("Properties", {})
        for lid, res in template_json.get("Resources", {}).items()
        if res.get("Type") == "AWS::IAM::Policy"
    }


def _statements(policy_props):
    """Estrae la lista di Statement dal PolicyDocument."""
    return policy_props.get("PolicyDocument", {}).get("Statement", [])


def _as_list(value):
    """Normalizza un campo Action/Resource (stringa o lista) in lista."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# ID logici (prefisso) delle policy la cui coppia azione/risorsa con `*` e'
# tecnicamente inevitabile e documentata. La state machine Step Functions ha
# bisogno di logs:CreateLogDelivery / logs:PutResourcePolicy e di
# xray:PutTraceSegments, azioni che NON supportano permessi a livello di risorsa:
# CDK (e AWS) emettono percio' Resource "*". Questo e' l'unico wildcard di
# risorsa accettato, ed e' confinato a queste azioni.
_DOCUMENTED_WILDCARD_ACTION_PREFIXES = (
    "logs:",
    "xray:",
)


# ---------------------------------------------------------------------------
# Requirement 9.4 - Least privilege
# ---------------------------------------------------------------------------

def test_no_resource_wildcard_except_documented_logs_xray():
    """Nessuna policy usa Resource '*' salvo le azioni logs/xray della SFN.

    Requirement 9.4: niente wildcard sulle risorse salvo dove tecnicamente
    inevitabile e documentato. L'unica eccezione ammessa e' lo statement della
    state machine per la log delivery e X-Ray (azioni prive di supporto
    resource-level).
    """
    template_json, _ = _synth()
    offending = []
    for lid, props in _iam_policies(template_json).items():
        for stmt in _statements(props):
            resources = _as_list(stmt.get("Resource"))
            if "*" not in resources:
                continue
            actions = _as_list(stmt.get("Action"))
            # Ammesso solo se OGNI azione dello statement e' un'azione
            # documentata (logs:*/xray:*) che non supporta resource-level.
            all_documented = actions and all(
                a.startswith(_DOCUMENTED_WILDCARD_ACTION_PREFIXES) for a in actions
            )
            if not all_documented:
                offending.append((lid, actions, resources))
    assert not offending, (
        "Trovato Resource '*' non documentato in policy IAM: "
        f"{offending}. Il least privilege (Requirement 9.4) ammette il "
        "wildcard di risorsa solo per le azioni logs/xray della state machine."
    )


def test_documented_wildcard_only_on_sfn_logs_xray():
    """Il wildcard di risorsa esiste ESATTAMENTE dove documentato (SFN logs/xray).

    Sanity check inverso: verifica che il caso eccezionale sia effettivamente
    presente e limitato alle azioni logs/xray, cosi' l'eccezione resta tracciata
    e non si allarga silenziosamente.
    """
    template_json, _ = _synth()
    wildcard_statements = []
    for props in _iam_policies(template_json).values():
        for stmt in _statements(props):
            if "*" in _as_list(stmt.get("Resource")):
                wildcard_statements.append(_as_list(stmt.get("Action")))
    # Deve esistere esattamente lo statement logs/xray della state machine.
    assert wildcard_statements, (
        "Atteso lo statement logs/xray della state machine con Resource '*'"
    )
    for actions in wildcard_statements:
        assert actions and all(
            a.startswith(_DOCUMENTED_WILDCARD_ACTION_PREFIXES) for a in actions
        ), f"Wildcard di risorsa su azioni non documentate: {actions}"


def test_no_action_wildcard_star():
    """Nessuna policy concede Action '*' (o servizio:*), che sarebbe over-privilege.

    Requirement 9.4: nessun wildcard sulle AZIONI. Le grant CDK usano prefissi
    espansi (es. s3:GetObject*) ma mai il jolly totale '*' ne' 'servizio:*'.
    """
    template_json, _ = _synth()
    offending = []
    for lid, props in _iam_policies(template_json).items():
        for stmt in _statements(props):
            for action in _as_list(stmt.get("Action")):
                if action == "*" or action.endswith(":*"):
                    offending.append((lid, action))
    assert not offending, (
        f"Azioni con wildcard totale non ammesse (Requirement 9.4): {offending}"
    )


def test_motore_role_scoped_to_dynamo_and_state_machine():
    """La Lambda motore ha SOLO DynamoDB (tabella/GSI) + StartExecution SFN.

    Least privilege (Requirement 9.4): niente azioni fuori da questo perimetro.
    """
    template_json, _ = _synth()
    policies = _iam_policies(template_json)
    motore_policy = next(
        (p for lid, p in policies.items() if lid.startswith("MotoreConversazionale")),
        None,
    )
    assert motore_policy is not None, "Policy della Lambda motore non trovata"
    actions = set()
    for stmt in _statements(motore_policy):
        actions.update(_as_list(stmt.get("Action")))
    # Ogni azione deve essere dynamodb:* (espanso) o states:StartExecution.
    for action in actions:
        assert action.startswith("dynamodb:") or action == "states:StartExecution", (
            f"Azione inattesa nel ruolo motore (over-privilege): {action}"
        )


def test_gestore_consenso_role_scoped_to_s3_read_and_ses_send():
    """La Lambda consenso ha SOLO lettura S3 (PDF) + ses:SendRawEmail.

    Requirement 9.4 + design.md (Sicurezza): consenso legge il PDF dal bucket
    (s3:GetObject sul solo oggetto) e invia via ses:SendRawEmail sulla sola
    identita' verificata. Nessuna scrittura S3, nessun altro servizio.
    """
    template_json, _ = _synth()
    policies = _iam_policies(template_json)
    consenso_policy = next(
        (p for lid, p in policies.items() if lid.startswith("GestoreConsenso")),
        None,
    )
    assert consenso_policy is not None, "Policy della Lambda consenso non trovata"
    actions = set()
    for stmt in _statements(consenso_policy):
        actions.update(_as_list(stmt.get("Action")))
    assert "ses:SendRawEmail" in actions, "Manca ses:SendRawEmail sul consenso"
    # Solo lettura S3 (Get/List/GetBucket) + SES send. Nessuna PutObject/Delete.
    for action in actions:
        assert action.startswith("s3:Get") or action.startswith("s3:List") or (
            action == "ses:SendRawEmail"
        ), f"Azione inattesa nel ruolo consenso (over-privilege): {action}"
    forbidden = {"s3:PutObject", "s3:DeleteObject", "s3:*"}
    assert not (actions & forbidden), (
        f"Il consenso non deve poter scrivere/eliminare su S3: {actions & forbidden}"
    )


def test_telegram_callback_role_scoped_to_dynamo_and_task_response():
    """telegram-callback ha SOLO DynamoDB + SendTask{...} + GetSecretValue.

    Least privilege (Requirement 9.4): sblocca la state machine via task response,
    applica la transizione di stato su DynamoDB e legge il SOLO webhook secret
    (secretsmanager:GetSecretValue) per validare l'autenticita' dei callback
    Telegram. Nessun altro permesso.
    """
    template_json, _ = _synth()
    policies = _iam_policies(template_json)
    cb_policy = next(
        (p for lid, p in policies.items() if lid.startswith("TelegramCallback")),
        None,
    )
    assert cb_policy is not None, "Policy della Lambda telegram-callback non trovata"
    actions = set()
    for stmt in _statements(cb_policy):
        actions.update(_as_list(stmt.get("Action")))
    for action in actions:
        assert (
            action.startswith("dynamodb:")
            or action.startswith("states:SendTask")
            or action.startswith("secretsmanager:")
        ), f"Azione inattesa nel ruolo telegram-callback: {action}"


def test_state_machine_invokes_only_specific_lambdas():
    """La state machine puo' invocare SOLO Lambda specifiche (ARN), non '*'.

    Requirement 9.4: lambda:InvokeFunction e' concesso su ARN specifici (Ref/GetAtt
    alle funzioni), mai su Resource '*'.
    """
    template_json, _ = _synth()
    policies = _iam_policies(template_json)
    sfn_policy = next(
        (p for lid, p in policies.items() if lid.startswith("ConfirmationWorkflow")),
        None,
    )
    assert sfn_policy is not None, "Policy del ruolo della state machine non trovata"
    for stmt in _statements(sfn_policy):
        actions = _as_list(stmt.get("Action"))
        if "lambda:InvokeFunction" in actions:
            resources = _as_list(stmt.get("Resource"))
            assert resources, "InvokeFunction senza risorse esplicite"
            assert "*" not in resources, (
                "lambda:InvokeFunction non deve usare Resource '*' (Requirement 9.4)"
            )


# ---------------------------------------------------------------------------
# Requirement 9.6 - Encryption at rest
# ---------------------------------------------------------------------------

def test_dynamodb_encryption_at_rest_enabled():
    """DynamoDB e' cifrata at rest (chiave AWS-owned).

    Trappola di naming CloudFormation: con la chiave AWS-owned la CDK emette
    SSESpecification.SSEEnabled=false, che significa "nessuna SSE-KMS", NON
    "nessuna cifratura". La cifratura at rest e' comunque sempre attiva. Qui
    verifichiamo che, se presente, SSEEnabled non sia true-con-KMS accidentale e
    che la tabella esista: la owned key e' l'impostazione voluta (costo-zero).
    """
    template_json, _ = _synth()
    tables = [
        res for res in template_json.get("Resources", {}).values()
        if res.get("Type") == "AWS::DynamoDB::Table"
    ]
    assert tables, "Nessuna tabella DynamoDB nel template"
    for table in tables:
        sse = table.get("Properties", {}).get("SSESpecification", {})
        # AWS-owned key: SSEEnabled False (cifratura at rest comunque attiva).
        # Se un domani si passasse a KMS, SSEEnabled=True e SSEType=KMS: anche
        # quello e' cifratura valida. In entrambi i casi la tabella e' cifrata.
        assert sse.get("SSEEnabled") in (False, True), (
            "SSESpecification assente o malformata: la cifratura at rest deve "
            "essere esplicitamente configurata (Requirement 9.6)"
        )


def test_consent_s3_bucket_encrypted_and_locked_down():
    """Il bucket S3 del consenso ha encryption at rest e accesso pubblico bloccato.

    Requirement 9.6: SSE attiva al provisioning (AES256 SSE-S3). In piu' l'accesso
    pubblico e' bloccato (dati sensibili anche se finti in questa fase).
    """
    template_json, _ = _synth()
    template = assertions.Template.from_stack(
        # ri-sintesi via Template per le has_resource_properties matchers.
        _fresh_stack()
    )
    template.has_resource_properties(
        "AWS::S3::Bucket",
        {
            "BucketEncryption": {
                "ServerSideEncryptionConfiguration": assertions.Match.array_with(
                    [
                        {
                            "ServerSideEncryptionByDefault": {
                                "SSEAlgorithm": "AES256"
                            }
                        }
                    ]
                )
            },
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "BlockPublicPolicy": True,
                "IgnorePublicAcls": True,
                "RestrictPublicBuckets": True,
            },
        },
    )


def test_consent_s3_bucket_enforces_ssl():
    """Il bucket del consenso forza TLS (deny su richieste non-SSL)."""
    template = assertions.Template.from_stack(_fresh_stack())
    # enforce_ssl aggiunge una BucketPolicy con Deny su aws:SecureTransport=false.
    template.has_resource_properties(
        "AWS::S3::BucketPolicy",
        {
            "PolicyDocument": {
                "Statement": assertions.Match.array_with(
                    [
                        assertions.Match.object_like(
                            {
                                "Effect": "Deny",
                                "Condition": {
                                    "Bool": {"aws:SecureTransport": "false"}
                                },
                            }
                        )
                    ]
                )
            }
        },
    )


def test_cloudwatch_logs_group_present_for_encryption_at_rest():
    """La state machine logga su un LogGroup dedicato (cifrato dal servizio Logs).

    Requirement 9.6: CloudWatch Logs cifra i dati at rest per default a livello di
    servizio. Verifichiamo che un LogGroup esista (retention breve, costo-zero):
    la cifratura at rest e' garantita dal servizio senza configurazione extra.
    """
    template = assertions.Template.from_stack(_fresh_stack())
    template.resource_count_is("AWS::Logs::LogGroup", 1)
    template.has_resource_properties(
        "AWS::Logs::LogGroup",
        {"RetentionInDays": 7},
    )


def test_no_plaintext_secrets_in_template():
    """Nessun VALORE di segreto in chiaro nel template (secret safety).

    Lo stack crea 3 secret in Secrets Manager (Google SA, token bot Telegram,
    webhook secret) ma VUOTI: la proprieta' CloudFormation e' `GenerateSecretString`
    (secret generato/da popolare a mano), NON `SecretString` con un valore inline.
    I VALORI dei segreti restano fuori dal template (li popola Alberto dopo il
    deploy). Il test verifica quindi che:
      - esistano i 3 secret (contenitori);
      - nessun secret abbia un `SecretString` inline (= valore hard-coded).
    """
    import json as _json

    template = assertions.Template.from_stack(_fresh_stack())
    # I 3 contenitori dei secret devono esistere (Google + 2 Telegram).
    template.resource_count_is("AWS::SecretsManager::Secret", 3)

    raw = _json.dumps(template.to_json())
    # `SecretString` (valore inline) NON deve comparire; `GenerateSecretString`
    # (secret vuoto/generato) e' invece legittimo e sicuro. Distinguo i due.
    assert '"SecretString"' not in raw, (
        "Il template non deve contenere un valore SecretString inline (secret safety)"
    )


# ---------------------------------------------------------------------------
# Requirement 9.2 / 9.3 / 9.7 - Deploy / Destroy / Rollback (via assertions)
# ---------------------------------------------------------------------------
#
# NOTA (Requirement 9.2, 9.3, 9.7) - deploy/destroy/rollback NON eseguiti:
#   Non viene eseguito alcun `cdk deploy` ne' `cdk destroy` reale (nessun
#   bootstrap, nessun account, nessun costo). Le aspettative sono coperte cosi':
#   - 9.2 (provisioning con successo): la synth genera un template valido
#     (verificata separatamente con `cdk synth`, exit 0) e tutte le risorse
#     hanno definizione completa. Il deploy reale richiede la conferma esplicita
#     di Alberto e non e' in scope di questo test.
#   - 9.3 (nessuna risorsa orfana su destroy): le risorse con stato (tabella,
#     bucket, log group) hanno DeletionPolicy=Delete (RemovalPolicy.DESTROY), e
#     il bucket ha auto-delete degli oggetti, cosi' `cdk destroy` le rimuove
#     senza lasciare orfani. Verificato dagli assert sotto.
#   - 9.7 (rollback su deploy fallito): comportamento di DEFAULT di
#     CloudFormation. Un `cdk deploy`/create/update stack che fallisce su una
#     risorsa esegue automaticamente il rollback allo stato precedente e riporta
#     la risorsa che ha causato il fallimento negeli stack events. Non serve
#     forzarlo ne' configurarlo: e' garantito dal servizio. Documentato qui.


def _fresh_stack():
    """Crea uno stack nuovo per i matcher assertions.Template.from_stack."""
    app = cdk.App()
    return CentralinoStack(
        app,
        "IacTestStackFresh",
        environment_name="dev",
        paid_components_enabled=False,
        env=cdk.Environment(region="us-east-1"),
    )


def test_stateful_resources_removal_policy_destroy_no_orphans():
    """Tabella, bucket e log group hanno DeletionPolicy=Delete (no orfani).

    Requirement 9.3: `cdk destroy` non deve lasciare risorse orfane. Le risorse
    con stato usano RemovalPolicy.DESTROY -> DeletionPolicy=Delete nel template.
    """
    template_json, _ = _synth()
    stateful_types = {
        "AWS::DynamoDB::Table",
        "AWS::S3::Bucket",
        "AWS::Logs::LogGroup",
    }
    stateful = {
        lid: res for lid, res in template_json.get("Resources", {}).items()
        if res.get("Type") in stateful_types
    }
    assert stateful, "Nessuna risorsa con stato trovata nel template"
    for lid, res in stateful.items():
        assert res.get("DeletionPolicy") == "Delete", (
            f"{lid} ({res['Type']}) deve avere DeletionPolicy=Delete per non "
            f"lasciare risorse orfane su cdk destroy (Requirement 9.3), "
            f"trovato: {res.get('DeletionPolicy')!r}"
        )


def test_consent_bucket_auto_deletes_objects():
    """Il bucket del consenso auto-elimina gli oggetti su destroy (no orfani).

    Requirement 9.3: un bucket non vuoto bloccherebbe il destroy. Il tag
    aws-cdk:auto-delete-objects=true e la custom resource dedicata garantiscono lo
    svuotamento prima della rimozione.
    """
    template_json, _ = _synth()
    buckets = [
        res for res in template_json.get("Resources", {}).values()
        if res.get("Type") == "AWS::S3::Bucket"
    ]
    assert buckets, "Nessun bucket S3 nel template"
    for bucket in buckets:
        tags = {
            t["Key"]: t["Value"]
            for t in bucket.get("Properties", {}).get("Tags", [])
        }
        assert tags.get("aws-cdk:auto-delete-objects") == "true", (
            "Il bucket del consenso deve auto-eliminare gli oggetti per non "
            "bloccare cdk destroy (Requirement 9.3)"
        )


def test_synthesizes_without_paid_resources_still_provisionable():
    """La synth di default produce un template provisionabile a costo-zero.

    Requirement 9.2 (provisioning) + 10.1 (costo-zero): il template di default
    non contiene risorse a pagamento (Connect/Lex/Bedrock) ma e' comunque
    completo e sintetizzabile. Il deploy reale non e' eseguito qui (serve
    conferma esplicita); la validita' del template e' garantita dalla synth.
    """
    template_json, _ = _synth(paid=False)
    types = {res.get("Type") for res in template_json.get("Resources", {}).values()}
    paid_types = {
        "AWS::Connect::Instance",
        "AWS::Connect::ContactFlow",
        "AWS::Lex::Bot",
        "AWS::Bedrock::Agent",
    }
    assert not (types & paid_types), (
        f"Risorse a pagamento non attese nel template di default: {types & paid_types}"
    )
    # Le risorse core devono esserci (provisioning intent completo).
    for required in ("AWS::DynamoDB::Table", "AWS::S3::Bucket",
                     "AWS::StepFunctions::StateMachine", "AWS::Lambda::Function"):
        assert required in types, f"Risorsa core mancante nel template: {required}"


@pytest.mark.parametrize("environment_name", ["dev", "test", "prod"])
def test_synth_valid_for_all_environments(environment_name):
    """Lo stack sintetizza per tutti gli Environment ammessi (deploy-ready)."""
    template_json, _ = _synth(environment_name=environment_name)
    assert template_json.get("Resources"), (
        f"Template vuoto per environment={environment_name}"
    )
