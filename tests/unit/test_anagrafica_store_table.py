"""Test IaC (Task 4) - CDK assertions sulla tabella DynamoDB Anagrafica_Store.

Protegge lo schema single-table (design.md - Data Models), allineato a
src/models.py, cosi' che la repository (Task 5) e le lambda successive possano
contare su chiavi/indici stabili:
- Tabella `centralino-medico` con PK/SK (String).
- GSI1 (GSI1PK/GSI1SK) - ricerca paziente per nome (Requirement 4.4).
- GSI2 (GSI2PK/GSI2SK) - appuntamenti per stato.
- Billing mode PAY_PER_REQUEST (Requirement 10.1, costo-zero).
- Encryption at rest attiva (Requirement 9.6): chiave DynamoDB-owned (nessun
  costo KMS). In CloudFormation questo significa nessun SSESpecification con
  SSEEnabled=false; la cifratura at rest resta comunque sempre attiva.
- RemovalPolicy DESTROY (dev/portfolio costo-zero): DeletionPolicy = Delete.

Tutti i test lavorano offline sul template sintetizzato: nessun deploy, nessun
costo.
"""
import aws_cdk as cdk
from aws_cdk import assertions

from stacks.centralino_stack import CentralinoStack

TABLE_TYPE = "AWS::DynamoDB::Table"
EXPECTED_TABLE_NAME = "centralino-medico"


def _synth_template(paid: bool = False, environment_name: str = "dev"):
    app = cdk.App()
    stack = CentralinoStack(
        app,
        "TestCentralinoStack",
        environment_name=environment_name,
        paid_components_enabled=paid,
        env=cdk.Environment(region="us-east-1"),
    )
    return assertions.Template.from_stack(stack), stack


def _table_resource(template):
    """Ritorna (logical_id, resource_dict) dell'unica tabella DynamoDB."""
    resources = template.to_json().get("Resources", {})
    tables = {
        lid: res
        for lid, res in resources.items()
        if res.get("Type") == TABLE_TYPE
    }
    assert len(tables) == 1, f"Attesa 1 tabella DynamoDB, trovate {len(tables)}"
    return next(iter(tables.items()))


# --- Esistenza e nome tabella ---

def test_table_exists_with_expected_name():
    template, _ = _synth_template()
    template.resource_count_is(TABLE_TYPE, 1)
    template.has_resource_properties(
        TABLE_TYPE, {"TableName": EXPECTED_TABLE_NAME}
    )


def test_table_exposed_as_stack_attribute():
    _, stack = _synth_template()
    assert hasattr(stack, "table"), "Lo stack deve esporre self.table"
    assert stack.table is not None


# --- Chiavi primarie PK/SK (String) ---

def test_primary_key_pk_sk_strings():
    template, _ = _synth_template()
    template.has_resource_properties(
        TABLE_TYPE,
        {
            "KeySchema": [
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            "AttributeDefinitions": assertions.Match.array_with(
                [
                    {"AttributeName": "PK", "AttributeType": "S"},
                    {"AttributeName": "SK", "AttributeType": "S"},
                ]
            ),
        },
    )


# --- GSI1 (ricerca per nome) e GSI2 (appuntamenti per stato) ---

def test_gsi1_and_gsi2_present_with_expected_keys():
    template, _ = _synth_template()
    template.has_resource_properties(
        TABLE_TYPE,
        {
            "GlobalSecondaryIndexes": assertions.Match.array_with(
                [
                    {
                        "IndexName": "GSI1",
                        "KeySchema": [
                            {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                            {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                    {
                        "IndexName": "GSI2",
                        "KeySchema": [
                            {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                            {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                        ],
                        "Projection": {"ProjectionType": "ALL"},
                    },
                ]
            ),
        },
    )


def test_gsi_attribute_definitions_declared_as_strings():
    """Gli attributi dei GSI devono comparire tra le AttributeDefinitions (S)."""
    template, _ = _synth_template()
    template.has_resource_properties(
        TABLE_TYPE,
        {
            "AttributeDefinitions": assertions.Match.array_with(
                [
                    {"AttributeName": "GSI1PK", "AttributeType": "S"},
                    {"AttributeName": "GSI1SK", "AttributeType": "S"},
                    {"AttributeName": "GSI2PK", "AttributeType": "S"},
                    {"AttributeName": "GSI2SK", "AttributeType": "S"},
                ]
            ),
        },
    )


# --- Billing mode PAY_PER_REQUEST (Requirement 10.1) ---

def test_billing_mode_pay_per_request():
    template, _ = _synth_template()
    template.has_resource_properties(
        TABLE_TYPE, {"BillingMode": "PAY_PER_REQUEST"}
    )


# --- Encryption at rest attiva (Requirement 9.6) ---

def test_encryption_at_rest_active_with_owned_key():
    """Chiave DynamoDB-owned (costo-zero): la cifratura at rest e' attiva.

    IMPORTANTE (trappola di naming CloudFormation): con TableEncryption.DEFAULT
    la CDK emette SSESpecification.SSEEnabled = false. Questo NON significa
    "nessuna cifratura": significa "nessuna SSE basata su KMS" e quindi si usa
    la AWS-owned key. La cifratura at rest E' comunque sempre attiva su tutte le
    tabelle DynamoDB, a costo zero (nessun addebito KMS). Vedi CFN docs:
    SSEEnabled=false -> server-side encryption impostata su AWS owned key.

    Il test verifica quindi che l'encryption sia con AWS-owned key: o
    SSESpecification assente, o presente con SSEEnabled=false SENZA un
    SSEType=KMS (che comporterebbe costi KMS, contro il costo-zero).
    """
    template, _ = _synth_template()
    _, resource = _table_resource(template)
    props = resource.get("Properties", {})
    sse = props.get("SSESpecification")
    if sse is not None:
        # Con la AWS-owned key non deve comparire un SSEType KMS (che sarebbe a
        # pagamento). SSEEnabled=false qui indica proprio la owned key gratuita.
        assert sse.get("SSEType") != "KMS", (
            "Con la owned key (costo-zero) non deve esserci SSEType=KMS"
        )


def test_no_kms_key_created_for_table():
    """Costo-zero: la owned key non crea alcuna KMS key nel template.

    Doppia garanzia rispetto al costo: l'uso della AWS-owned key
    (TableEncryption.DEFAULT) non deve generare risorse AWS::KMS::Key, che
    comporterebbero addebiti KMS. La tabella non deve esporre una
    encryption_key (owned key gestita internamente da DynamoDB).
    """
    template, stack = _synth_template()
    template.resource_count_is("AWS::KMS::Key", 0)
    assert stack.table.encryption_key is None


# --- RemovalPolicy DESTROY (dev/costo-zero) ---

def test_table_removal_policy_destroy():
    template, _ = _synth_template()
    _, resource = _table_resource(template)
    assert resource.get("DeletionPolicy") == "Delete", (
        "La tabella deve avere RemovalPolicy DESTROY (DeletionPolicy=Delete) "
        "per un progetto dev/portfolio costo-zero"
    )


# --- Non deve rompere gli assert di Task 2 (tag obbligatori sulla tabella) ---

def test_table_carries_mandatory_tags():
    template, _ = _synth_template()
    _, resource = _table_resource(template)
    tags = {t["Key"]: t["Value"] for t in resource.get("Properties", {}).get("Tags", [])}
    assert tags.get("Project") == "centralino-ai-studio-medico"
    assert tags.get("Owner") == "savinoas"
    assert tags.get("Environment") == "dev"
