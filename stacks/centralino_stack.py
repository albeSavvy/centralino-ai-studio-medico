"""Stack principale - Centralino AI Studio Medico (Idelia).

Task 2 (IaC di base):
- Tag obbligatori a livello di stack applicati a tutte le risorse taggabili
  (Project, Environment, Owner=savinoas) via Tags.of(self).
- Billing alarm a soglia 1 USD con canale di notifica SNS.
- Flag/context CDK `paid_components_enabled` (default False) che gate-a i
  componenti a pagamento (Amazon Connect, Amazon Bedrock reale), introdotti
  nelle task successive.

Costo-zero: qui si DEFINISCE solo l'infrastruttura (template CloudFormation).
Nessun deploy, nessun bootstrap, nessuna risorsa a pagamento provisionata.

Scelta billing alarm: allarme CloudWatch sulla metrica AWS/Billing
`EstimatedCharges` (valuta USD). Questa metrica e' pubblicata SOLO nella region
us-east-1: lo stack va quindi deployato in us-east-1 perche' l'allarme funzioni.
Il vincolo e' documentato ed esplicitato piu' sotto.
"""
from __future__ import annotations

import json
import os

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    Tags,
    aws_apigatewayv2 as apigwv2,
    aws_apigatewayv2_integrations as apigwv2_integrations,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_connect as connect,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lex as lex,
    aws_logs as logs,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_sns as sns,
    aws_stepfunctions as sfn,
    aws_stepfunctions_tasks as sfn_tasks,
)
from constructs import Construct

# Valori ammessi per il tag Environment (Requirement 9.5).
_VALID_ENVIRONMENTS = {"dev", "test", "prod"}

# Tag obbligatori (Requirement 9.5). Owner e' fisso; Project e Environment
# sono applicati dinamicamente nel costruttore.
_PROJECT_TAG = "centralino-ai-studio-medico"
_OWNER_TAG = "savinoas"

# Soglia billing alarm in USD (Requirement 10.4).
_BILLING_THRESHOLD_USD = 1

# Region in cui e' disponibile la metrica di billing AWS/Billing.
_BILLING_METRIC_REGION = "us-east-1"

# --- Anagrafica_Store: nome tabella e nomi degli attributi chiave ---
# Il nome usa hyphen (nessun em dash), coerente con la convenzione naming.
_TABLE_NAME = "centralino-medico"
# Chiavi primarie single-table (design.md - Data Models).
_PK = "PK"
_SK = "SK"
# GSI1: ricerca paziente per nome (GSI1PK = PNAME#<nome_normalizzato>).
_GSI1_NAME = "GSI1"
_GSI1_PK = "GSI1PK"
_GSI1_SK = "GSI1SK"
# GSI2: appuntamenti per stato (GSI2PK = APPTSTATUS#<stato>, GSI2SK = <startIso>).
_GSI2_NAME = "GSI2"
_GSI2_PK = "GSI2PK"
_GSI2_SK = "GSI2SK"

# --- Motore_Conversazionale + simulatore canale testo (Task 14/15) ---
# Radice del progetto (contiene src/ e lambdas/): il codice della Lambda e'
# impacchettato da qui. __file__ e' stacks/centralino_stack.py -> risaliamo di 1.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Runtime Python della Lambda (allineato all'ambiente di sviluppo/test).
_LAMBDA_RUNTIME = lambda_.Runtime.PYTHON_3_12

# Layer con le dipendenze Google (google-api-python-client, google-auth) per la
# Lambda scrivi-calendario. Le wheel sono installate in layers/google/python/
# mirando a manylinux2014_x86_64 + cp312 (compatibili col runtime Lambda Linux),
# non ai binari Windows locali. Vedi docs/setup-manuale.md per il comando pip.
_GOOGLE_LAYER_DIR = os.path.join(_PROJECT_ROOT, "layers", "google")
# Handler: modulo.funzione dentro lambdas/ (thin handler che delega a src/motore).
_MOTORE_HANDLER = "lambdas.motore_conversazionale.handler"
# Nome env var lette dal thin handler (devono combaciare con lambdas/).
_ENV_PAID_COMPONENTS = "PAID_COMPONENTS_ENABLED"
_ENV_TABLE_NAME = "CENTRALINO_TABLE"
# Percorso HTTP del simulatore canale testo (design.md - Simulatore canale testo).
_SIMULATE_ROUTE = "/simulate"

# --- Orchestrazione Finestra_Conferma 8h: Step Functions (Task 11) ---
# design.md - Decisione 4. Componente Free-Tier-friendly: Standard Workflows
# hanno un free tier di 4000 transizioni/mese; qui si DEFINISCE solo la macchina.
# Handler delle Lambda della state machine (thin -> src/...).
_NOTIFICATORE_HANDLER = "lambdas.notificatore_telegram.handler"
_SCRIVI_CALENDARIO_HANDLER = "lambdas.scrivi_calendario.handler"
_GESTORE_CONSENSO_HANDLER = "lambdas.gestore_consenso.handler"
_TELEGRAM_CALLBACK_HANDLER = "lambdas.telegram_callback.handler"

# --- Gestore_Consenso: bucket S3 del PDF di consenso + env (Task 12/18) ---
# Il PDF del modulo di consenso vive in un bucket S3 con encryption at rest
# attiva (Requirement 9.6). Il gestore-consenso lo legge a runtime (s3:GetObject
# sul solo oggetto) e lo allega alla mail SES. Nomi delle env var allineati a
# lambdas/gestore_consenso.py (ENV_PDF_BUCKET / ENV_PDF_KEY / ENV_SENDER_EMAIL).
_ENV_CONSENSO_PDF_BUCKET = "CONSENSO_PDF_BUCKET"
_ENV_CONSENSO_PDF_KEY = "CONSENSO_PDF_KEY"
_ENV_CONSENSO_SENDER_EMAIL = "CONSENSO_SENDER_EMAIL"
# Chiave (object key) del PDF di consenso dentro il bucket.
_CONSENSO_PDF_KEY = "consenso/modulo-consenso.pdf"
# Mittente verificato SES per l'invio del consenso (dato finto, verificato in SES
# a deploy). Least privilege: ses:SendRawEmail e' concesso sulla sola identita'.
_CONSENSO_SENDER_EMAIL = "albe.savino@gmail.com"
# Destinatari di test verificati in SES (sandbox): SES richiede il permesso
# SendRawEmail anche sull'identita' del DESTINATARIO finche' e' in sandbox. In
# produzione (SES fuori sandbox) questa lista non servirebbe. Dati di test.
_CONSENSO_TEST_RECIPIENTS = [
    "savino.francesca82@gmail.com",
    "albe.savino@gmail.com",
]
# Finestra di conferma: 8 ore (Requirement 6.3, 6.4). Timeout ESPLICITO sul task
# di callback (waitForTaskToken), non HeartbeatSeconds (design.md - Decisione 4).
_CONFIRMATION_WINDOW = Duration.hours(8)
# Scrittura calendario: max 3 tentativi, intervallo >= 2s (Requirement 7.6).
_CALENDAR_MAX_ATTEMPTS = 3
_CALENDAR_RETRY_INTERVAL = Duration.seconds(2)
# Nomi degli stati appuntamento (allineati a src/models.ApptStatus) usati nei
# Pass state che annotano l'esito del ramo.
_STATUS_CONFERMATO = "CONFERMATO"
_STATUS_CONFERMATO_AUTO = "CONFERMATO_AUTO"
_STATUS_DA_RIVEDERE = "DA_RIVEDERE"
_STATUS_CALENDARIO_FALLITO = "SCRITTURA_CALENDARIO_FALLITA"
# Percorso HTTP del webhook Telegram (callback bottoni -> telegram-callback).
_TELEGRAM_WEBHOOK_ROUTE = "/telegram/webhook"
# Retention dei log della state machine (costo-zero: retention breve).
_SFN_LOG_RETENTION = logs.RetentionDays.ONE_WEEK

# --- Servizi esterni: Google Calendar + Telegram (Fasi C/D del setup) ---
# I VALORI dei segreti (chiave JSON Service Account, token bot, webhook secret)
# NON sono nel codice: lo stack crea i secret VUOTI in Secrets Manager e Alberto
# li popola a mano dopo il deploy (secret-safety: nessun segreto in CDK/repo).
# Alle Lambda passiamo solo il NOME del secret (env) e i valori NON segreti
# (calendarId, chat_id). Nomi env allineati agli handler in lambdas/.
_ENV_GOOGLE_SA_SECRET_NAME = "GOOGLE_SA_SECRET_NAME"
_ENV_GOOGLE_CALENDAR_IDS = "GOOGLE_CALENDAR_IDS"
_ENV_TELEGRAM_BOT_TOKEN_SECRET_NAME = "TELEGRAM_BOT_TOKEN_SECRET_NAME"
_ENV_TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"
_ENV_TELEGRAM_WEBHOOK_SECRET_NAME = "TELEGRAM_WEBHOOK_SECRET_NAME"
# Modello Bedrock per il dialogo (tool-use via Converse). Nome env allineato a
# lambdas/motore_conversazionale.py (ENV_BEDROCK_MODEL_ID). Default: Claude 3.5
# Haiku (veloce/economico, supporta tool-use). Override via context CDK
# `-c bedrock_model_id=...` (utile se il default non e' abilitato in Model access
# o se serve un inference profile con prefisso regionale, es. us.anthropic...).
_ENV_BEDROCK_MODEL_ID = "BEDROCK_MODEL_ID"
_DEFAULT_BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# Nomi fisici dei secret in Secrets Manager (hyphen, no em dash).
_GOOGLE_SA_SECRET_NAME = "centralino/google-service-account"
_TELEGRAM_BOT_TOKEN_SECRET_NAME = "centralino/telegram-bot-token"
_TELEGRAM_WEBHOOK_SECRET_NAME = "centralino/telegram-webhook-secret"
# Mapping dottoressa -> calendarId Google (valori NON segreti, dati di test).
# Serializzato in JSON nell'env GOOGLE_CALENDAR_IDS letto da scrivi_calendario.
_GOOGLE_CALENDAR_IDS = {
    "chiara": (
        "3f9e2b3ecd90e4fac485e33217b39caac2fff6835ccf5bcacb7aaa44c96d275e"
        "@group.calendar.google.com"
    ),
    "francesca": (
        "b536086a083d609d6f006924f991f0564e71372ed58b7510974410dfca6c0415"
        "@group.calendar.google.com"
    ),
}
# Chat id del gruppo Telegram (valore NON segreto): default vuoto, si passa via
# context CDK (-c telegram_chat_id=...) al deploy con i servizi esterni.
_DEFAULT_TELEGRAM_CHAT_ID = ""

# --- Lex V2 strato voce (Task 16) - COMPONENTE A PAGAMENTO, gated dal flag ---
# Il bot vive solo quando paid_components_enabled=True (Requirement 10.2): la
# synth di default (flag=false) NON produce alcuna risorsa Lex (costo-zero).
# Locale italiano: Lex V2 usa il codice ICU it_IT (design.md - lingua italiano).
_LEX_LOCALE_IT = "it_IT"
# Soglia di confidenza NLU: sotto questa, Lex instrada alla FallbackIntent, che
# e' proprio il nostro catch-all verso la Lambda. Con 0.4 (min consigliato) ogni
# utterance non riconosciuta come intent specifico cade nel catch-all.
_LEX_NLU_THRESHOLD = 0.4
# Voce Polly italiana per il TTS del bot (barge-in/prompt gestiti in Task 17).
_LEX_VOICE_ID = "Bianca"
# TTL di sessione (secondi): finestra entro cui Lex mantiene lo stato dialogo.
_LEX_SESSION_TTL_S = 300
# Nome dell'intent catch-all: e' la FallbackIntent nativa di Lex, che cattura
# ogni utterance non mappata a un intent specifico e la inoltra alla Lambda.
_LEX_FALLBACK_INTENT = "CatchAllFallbackIntent"
# Signature dell'intent di fallback di Lex V2 (obbligatoria per il locale).
_LEX_FALLBACK_SIGNATURE = "AMAZON.FallbackIntent"

# --- Amazon Connect: contact flow inbound (Task 17) - A PAGAMENTO, gated ---
# Connect e' un componente a pagamento (numero telefonico + tariffa al minuto,
# Requirement 10.2): l'istanza, il contact flow, il numero e l'associazione al
# bot Lex sono creati SOLO quando self.paid_components_enabled e' True. Con il
# default (flag=false) la synth NON produce ALCUNA risorsa Connect (costo-zero,
# Requirement 10.1).
# Alias univoco dell'istanza Connect (deve essere globalmente univoco all'account).
_CONNECT_INSTANCE_ALIAS = "centralino-idelia"
# Messaggio di benvenuto vocale in italiano (Requirement 1.1). Testo TTS/Polly.
_CONNECT_WELCOME_IT = (
    "Studio di psicologia, sede di Meda. Sono l'assistente vocale. "
    "Mi dica pure come posso aiutarla."
)
# Prompt di ri-tentativo su silenzio (Requirement 1.4). Ripetuto max 3 volte.
_CONNECT_REPROMPT_IT = "Non ho sentito nulla. Puo' ripetere, per favore?"
# Messaggio di mancato riconoscimento / bassa confidenza (Requirement 1.5).
_CONNECT_LOW_CONFIDENCE_IT = (
    "Scusi, non ho capito bene. Puo' ripetere, per favore?"
)
# Messaggio di chiusura dopo l'esaurimento dei ri-prompt (Requirement 1.4).
_CONNECT_GOODBYE_IT = (
    "Non sono riuscito a sentirla. Richiami quando vuole. Arrivederci."
)
# Numero massimo di ri-prompt su silenzio prima di chiudere (Requirement 1.4).
_CONNECT_MAX_REPROMPTS = 3
# Timeout di attesa input del paziente in secondi (Requirement 1.4: 10s).
_CONNECT_INPUT_TIMEOUT_S = 10
# Esclusioni dell'asset Lambda: solo src/ e lambdas/ servono a runtime. Escludere
# le cartelle pesanti (venv, output CDK, cache, test, IaC) tiene l'asset piccolo e
# la synth veloce; il bundling piu' selettivo e' anche piu' pulito per il deploy.
_ASSET_EXCLUDE = [
    ".venv",
    "cdk.out",
    ".hypothesis",
    ".pytest_cache",
    "__pycache__",
    "tests",
    "docs",
    "stacks",
    "layers",
    "app.py",
    "cdk.json",
    ".git",
]


class CentralinoStack(Stack):
    """Stack di base con tagging, billing alarm e flag componenti a pagamento."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment_name: str = "dev",
        paid_components_enabled: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- Validazione Environment (Requirement 9.5) ---
        if environment_name not in _VALID_ENVIRONMENTS:
            raise ValueError(
                "environment_name deve essere uno tra "
                f"{sorted(_VALID_ENVIRONMENTS)}, ricevuto: {environment_name!r}"
            )

        # Flag componenti a pagamento (Requirement 10.2). Default False: i
        # componenti a pagamento (Connect, Bedrock reale) restano disattivati.
        # Esposto come attributo cosi' le task successive possono gate-are le
        # risorse a pagamento su questo valore.
        self.paid_components_enabled: bool = paid_components_enabled
        # Flag SEPARATO per la voce (Lex + Connect): default False. Cosi' Bedrock
        # (dialogo) si attiva con paid_components_enabled SENZA creare la voce.
        # Attivabile via context CDK `-c voice_enabled=true` quando si fara' la
        # fase voce (l'ultima, a pagamento).
        self.voice_enabled: bool = (
            str(self.node.try_get_context("voice_enabled")).lower() == "true"
        )
        # Flag GRANULARE per il solo bot Lex (senza Connect): permette di
        # deployare/testare lo strato Lex a tappe, prima di affrontare Connect
        # (che si configura in console). `voice_enabled=true` implica lex.
        self.lex_enabled: bool = self.voice_enabled or (
            str(self.node.try_get_context("lex_enabled")).lower() == "true"
        )

        # --- Tag obbligatori su tutte le risorse taggabili (Requirement 9.5) ---
        # Tags.of(self) propaga i tag a ogni risorsa taggabile dello stack.
        Tags.of(self).add("Project", _PROJECT_TAG)
        Tags.of(self).add("Environment", environment_name)
        Tags.of(self).add("Owner", _OWNER_TAG)

        # --- Billing alarm a 1 USD con notifica SNS (Requirement 10.4) ---
        self._create_billing_alarm(environment_name)

        # --- Anagrafica_Store: tabella DynamoDB single-table (Task 4) ---
        # Requirement 3.5 (persistenza/ricerca pazienti e appuntamenti),
        # Requirement 9.6 (encryption at rest). Espone self.table per le task
        # successive (repository, lambda) che vi faranno grant/reference.
        self._create_table()

        # --- Secret dei servizi esterni (Google, Telegram) in Secrets Manager ---
        # Creati VUOTI: Alberto popola i valori a mano dopo il deploy (secret
        # safety). Espone self.google_sa_secret / telegram_bot_secret /
        # telegram_webhook_secret per grant e env alle Lambda della workflow.
        # Il chat_id del gruppo Telegram (non segreto) arriva via context CDK.
        self.telegram_chat_id: str = (
            self.node.try_get_context("telegram_chat_id") or _DEFAULT_TELEGRAM_CHAT_ID
        )
        # Modello Bedrock: default Claude 3.5 Haiku, override via context.
        self.bedrock_model_id: str = (
            self.node.try_get_context("bedrock_model_id") or _DEFAULT_BEDROCK_MODEL_ID
        )
        self._create_external_secrets()

        # Layer condiviso con le dipendenze Google (googleapiclient, google-auth):
        # serve sia a scrivi-calendario sia al motore (che ora usa il
        # CalendarProvider reale per assegnare gli slot via tool-use Bedrock).
        self.google_layer = lambda_.LayerVersion(
            self,
            "GoogleDepsLayer",
            code=lambda_.Code.from_asset(_GOOGLE_LAYER_DIR),
            compatible_runtimes=[_LAMBDA_RUNTIME],
            description="google-api-python-client + google-auth (motore + scrivi-calendario).",
        )

        # --- Motore_Conversazionale (Lambda) + simulatore canale testo (Task 14/15) ---
        # Lambda thin che delega a src/motore.py; l'API Gateway `/simulate` la
        # invoca via canale testo (nessun numero reale, nessun costo vocale).
        # Bedrock resta mockato di default (PAID_COMPONENTS_ENABLED gate-ato dal
        # flag componenti a pagamento): il simulatore gira a costo zero.
        self._create_motore_and_simulator()

        # --- Orchestrazione Finestra_Conferma 8h: Step Functions (Task 11) ---
        # State machine confirmation-workflow (design.md - Decisione 4): invia
        # recap Telegram con taskToken (waitForTaskToken), race tra Wait(8h)->
        # ConfermatoAuto e callback Conferma/Blocker, poi scrittura calendario
        # (3 retry) -> consenso -> Completato; blocker -> DaRivedere. Le Lambda
        # notificatore-telegram / scrivi-calendario / gestore-consenso e la
        # Lambda telegram-callback (che sblocca via SendTaskSuccess) sono create
        # qui e cablate alla macchina. Standard Workflow: Free-Tier-friendly,
        # nessun costo a riposo; qui si DEFINISCE solo l'infrastruttura.
        self._create_confirmation_workflow()

        # --- Strato voce Lex V2 (Task 16) - A PAGAMENTO, gated dal flag ---
        # Amazon Lex e' un componente a pagamento (Requirement 10.2): il bot e
        # tutte le risorse correlate (ruolo Lex, alias, permission) sono create
        # SOLO quando self.paid_components_enabled e' True. Con il default
        # (flag=false) la synth NON produce alcuna risorsa Lex: costo-zero
        # (Requirement 10.1). Esposto self.lex_bot solo in quel caso.
        self.lex_bot = None
        self.lex_bot_alias = None
        self.lex_role = None
        # --- Amazon Connect: contact flow inbound (Task 17) - A PAGAMENTO ---
        # Connect (numero + minuti) e' gated dallo stesso flag: con il default
        # (flag=false) la synth NON produce alcuna risorsa Connect (costo-zero,
        # Requirement 10.1, 10.2). Esposti gli attributi solo quando attivo.
        self.connect_instance = None
        self.connect_contact_flow = None
        self.connect_phone_number = None
        self.connect_lex_association = None
        # La VOCE (Lex + Connect) e' gate-ata da un flag SEPARATO `voice_enabled`,
        # cosi' si puo' attivare Bedrock (dialogo, tramite paid_components_enabled)
        # SENZA creare anche Lex/Connect. La voce e' l'ultima fase, a parte.
        # VOCE (Lex + Connect): NON creata via CloudFormation. Il supporto CFN di
        # AWS::Lex::Bot (FallbackIntent) e di Connect (contact flow, numero) e'
        # parziale e problematico (es. il locale it_IT fallisce l'import della
        # fallback intent via CFN). Lex e Connect si creano quindi in CONSOLE, che
        # e' l'approccio "console-first" idiomatico per questi servizi. La Lambda
        # motore e' gia' pronta a ricevere gli eventi Lex V2 (adapter in
        # parse_request/build_response). I metodi _create_lex_voice_layer /
        # _create_connect_voice_layer restano nel codice come riferimento ma NON
        # sono invocati (via CFN falliscono). Vedi steering #project-centralino.
        _VOICE_VIA_CFN = False  # tenuto a False: voce creata in console.
        if _VOICE_VIA_CFN and self.lex_enabled:
            self._create_lex_voice_layer()
        if _VOICE_VIA_CFN and self.voice_enabled:
            self._create_connect_voice_layer()

        # NOTA: gli altri componenti a pagamento (Amazon Bedrock reale) seguono
        # lo stesso pattern: istanziati SOLO quando self.paid_components_enabled
        # e' True.

    def _create_billing_alarm(self, environment_name: str) -> None:
        """Crea il topic SNS e l'allarme CloudWatch sul costo stimato (1 USD).

        La metrica AWS/Billing EstimatedCharges e' pubblicata solo in
        us-east-1: se lo stack non e' in quella region, l'allarme e' comunque
        definito nel template ma non ricevera' datapoint. Il vincolo e'
        documentato; il deploy previsto e' in us-east-1.
        """
        # Canale di notifica: topic SNS a cui l'allarme pubblica.
        billing_topic = sns.Topic(
            self,
            "BillingAlarmTopic",
            display_name="centralino-billing-alarm",
        )

        # Avviso non bloccante in fase di synth se la region non e' us-east-1.
        if self.region and self.region != _BILLING_METRIC_REGION:
            # Non solleviamo eccezione: lo stack resta valido. La metrica di
            # billing pero' non produrra' dati fuori da us-east-1.
            pass

        estimated_charges = cloudwatch.Metric(
            namespace="AWS/Billing",
            metric_name="EstimatedCharges",
            dimensions_map={"Currency": "USD"},
            statistic="Maximum",
            # Periodo lungo: la metrica di billing e' aggiornata poche volte
            # al giorno. 6 ore e' un valore ragionevole.
            period=Duration.hours(6),
        )

        alarm = cloudwatch.Alarm(
            self,
            "BillingAlarm",
            alarm_name="centralino-billing-alarm-1usd",
            alarm_description=(
                "Costo stimato AWS oltre 1 USD per il progetto "
                "centralino-ai-studio-medico. Approccio costo-zero: la soglia "
                "e' volutamente bassa per intercettare qualsiasi spesa."
            ),
            metric=estimated_charges,
            threshold=_BILLING_THRESHOLD_USD,
            evaluation_periods=1,
            comparison_operator=(
                cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD
            ),
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alarm.add_alarm_action(cw_actions.SnsAction(billing_topic))

        # Esposti come attributi per riuso/test nelle task successive.
        self.billing_topic = billing_topic
        self.billing_alarm = alarm

    def _create_table(self) -> None:
        """Crea la tabella DynamoDB single-table Anagrafica_Store.

        Schema (design.md - Data Models, allineato a src/models.py):
          PK (String, partition) / SK (String, sort)   -> identita entita
          GSI1: GSI1PK (String) / GSI1SK (String)       -> ricerca per nome
                (GSI1PK = PNAME#<nome_normalizzato>, GSI1SK = PATIENT#<clientNo>)
          GSI2: GSI2PK (String) / GSI2SK (String)       -> appuntamenti per stato
                (GSI2PK = APPTSTATUS#<stato>, GSI2SK = <startIso>)

        Scelte:
        - Billing mode PAY_PER_REQUEST (on-demand): nessuna capacita
          pre-provisionata, ideale per volumi bassi e Free Tier (Requirement
          10.1). Nessun costo a riposo.
        - Encryption at rest ENABLED con chiave DynamoDB-owned
          (TableEncryption.DEFAULT). Tutte le tabelle DynamoDB sono cifrate at
          rest per default; la owned key non comporta costi KMS, coerente col
          costo-zero (Requirement 9.6). L'alternativa AWS_MANAGED userebbe una
          KMS key gestita (SSEType=KMS) con addebiti KMS: qui evitata.
          NOTA (trappola di naming CloudFormation): con DEFAULT la CDK emette
          SSESpecification.SSEEnabled=false. Questo NON significa "nessuna
          cifratura": indica solo "nessuna SSE KMS", quindi si usa la AWS-owned
          key e la cifratura at rest resta comunque sempre attiva.
        - RemovalPolicy DESTROY: progetto dev/portfolio costo-zero, cosi'
          `cdk destroy` rimuove la tabella senza lasciare risorse orfane.
          Da NON usare in produzione (rischio perdita dati).

        Entrambi i GSI usano projection ALL: gli item sono piccoli e le query
        (ricerca per nome, appuntamenti per stato) hanno bisogno dell'intero
        item; con volumi Free Tier il costo di storage duplicato e' trascurabile.
        """
        table = dynamodb.Table(
            self,
            "AnagraficaStore",
            table_name=_TABLE_NAME,
            partition_key=dynamodb.Attribute(
                name=_PK, type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name=_SK, type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            # Encryption at rest con chiave di proprieta' di DynamoDB
            # (nessun costo KMS): la cifratura at rest resta comunque attiva.
            encryption=dynamodb.TableEncryption.DEFAULT,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # GSI1: ricerca paziente per nome normalizzato (Requirement 4.4).
        table.add_global_secondary_index(
            index_name=_GSI1_NAME,
            partition_key=dynamodb.Attribute(
                name=_GSI1_PK, type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name=_GSI1_SK, type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # GSI2: appuntamenti per stato (es. tutti i PROVVISORIO), ordinati per
        # start (design.md - Chiavi e indici).
        table.add_global_secondary_index(
            index_name=_GSI2_NAME,
            partition_key=dynamodb.Attribute(
                name=_GSI2_PK, type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name=_GSI2_SK, type=dynamodb.AttributeType.STRING
            ),
            projection_type=dynamodb.ProjectionType.ALL,
        )

        # Esposta come attributo: le task successive (repository, lambda)
        # potranno referenziarla e ricevere grant di accesso least-privilege.
        self.table = table

    def _create_external_secrets(self) -> None:
        """Crea i secret dei servizi esterni (Google, Telegram) VUOTI.

        Secret safety (aws-agent-rules): lo stack crea i contenitori dei segreti
        ma NON i valori. Alberto li popola a mano dopo il deploy (console o CLI
        `put-secret-value`). Cosi' nessun valore di segreto entra mai nel codice,
        nel template CloudFormation o nel repo.

        Tre secret:
          - Google Service Account (chiave JSON) -> usato da scrivi-calendario
          - Telegram bot token -> usato da notificatore-telegram (invio recap)
          - Telegram webhook secret -> usato da telegram-callback (validazione)

        RemovalPolicy DESTROY: `cdk destroy` li rimuove (dev/costo-zero). In
        produzione andrebbe RETAIN. Costo: ~0,40 USD/secret al mese finche' lo
        stack e' attivo (Secrets Manager NON e' Free Tier).
        """
        self.google_sa_secret = secretsmanager.Secret(
            self,
            "GoogleServiceAccountSecret",
            secret_name=_GOOGLE_SA_SECRET_NAME,
            description="Chiave JSON del Service Account Google (popolare a mano).",
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.telegram_bot_secret = secretsmanager.Secret(
            self,
            "TelegramBotTokenSecret",
            secret_name=_TELEGRAM_BOT_TOKEN_SECRET_NAME,
            description="Token del bot Telegram da BotFather (popolare a mano).",
            removal_policy=RemovalPolicy.DESTROY,
        )
        self.telegram_webhook_secret = secretsmanager.Secret(
            self,
            "TelegramWebhookSecret",
            secret_name=_TELEGRAM_WEBHOOK_SECRET_NAME,
            description=(
                "Secret token del webhook Telegram (X-Telegram-Bot-Api-Secret-Token)."
            ),
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _create_motore_and_simulator(self) -> None:
        """Crea la Lambda motore-conversazionale e il simulatore canale testo.

        Task 14 (Lambda) + Task 15 (simulatore): un'unica Lambda
        `motore-conversazionale` (thin handler in lambdas/, logica in src/motore.py)
        serve sia lo strato voce (Connect/Lex, task future) sia il simulatore
        canale testo. Qui la esponiamo dietro un'API HTTP (`/simulate`) cosi'
        l'intero flusso e' eseguibile da CLI/test senza numero telefonico reale e
        senza costi vocali (Requirement 1.6, 10.1).

        Costo-zero (Requirement 10.1, 10.2):
        - `PAID_COMPONENTS_ENABLED` e' impostata da self.paid_components_enabled
          (default False), quindi il thin handler usa il client Bedrock MOCK:
          nessuna chiamata a Bedrock, nessun costo. Bedrock reale solo quando i
          componenti a pagamento sono esplicitamente attivati.
        - HTTP API (API Gateway v2) e Lambda rientrano nel Free Tier per i volumi
          di sviluppo/test; nessuna capacita' pre-provisionata.
        - Least privilege: alla Lambda si concede solo read/write sulla tabella
          single-table (SESSION + entita' correlate), nessun wildcard.

        Il codice della Lambda e' impacchettato dalla radice del progetto cosi'
        sia il package `src` sia `lambdas` sono disponibili a runtime; l'handler
        e' `lambdas.motore_conversazionale.handler`.
        """
        # Lambda motore-conversazionale (thin handler -> src/motore.py).
        motore_fn = lambda_.Function(
            self,
            "MotoreConversazionale",
            function_name="centralino-motore-conversazionale",
            runtime=_LAMBDA_RUNTIME,
            handler=_MOTORE_HANDLER,
            # Impacchetta la radice del progetto: include src/ e lambdas/. Le
            # cartelle non necessarie a runtime (venv, output CDK, cache, test,
            # IaC) sono escluse cosi' l'asset resta piccolo e la synth veloce.
            code=lambda_.Code.from_asset(_PROJECT_ROOT, exclude=_ASSET_EXCLUDE),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                # Bedrock mockato di default: il simulatore gira a costo zero.
                _ENV_PAID_COMPONENTS: str(self.paid_components_enabled).lower(),
                _ENV_TABLE_NAME: self.table.table_name,
            },
        )

        # Least privilege: solo read/write sulla tabella single-table (la Lambda
        # legge/scrive l'entita' SESSION ed esegue i tool su Anagrafica_Store).
        self.table.grant_read_write_data(motore_fn)

        # Tool-use Bedrock reale (Livello 2): il motore, quando il modello invoca
        # assegna_colloquio, usa il CalendarProvider Google per gli slot e avvia
        # la state machine. Servono: layer Google, env Google + modello Bedrock,
        # grant lettura secret Google + invoke Bedrock. (Il grant DynamoDB c'e'
        # gia' sopra; grant_start_execution + env ARN state machine sono aggiunti
        # in _create_confirmation_workflow.)
        motore_fn.add_layers(self.google_layer)
        motore_fn.add_environment(
            _ENV_GOOGLE_SA_SECRET_NAME, self.google_sa_secret.secret_name
        )
        import json as _json_motore
        motore_fn.add_environment(
            _ENV_GOOGLE_CALENDAR_IDS, _json_motore.dumps(_GOOGLE_CALENDAR_IDS)
        )
        motore_fn.add_environment(_ENV_BEDROCK_MODEL_ID, self.bedrock_model_id)
        self.google_sa_secret.grant_read(motore_fn)
        # Invocazione del modello Bedrock (Converse). Least privilege: solo
        # bedrock:InvokeModel sul modello configurato.
        motore_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=["*"],  # i model ARN variano per regione/versione
            )
        )

        # --- Simulatore canale testo: HTTP API con route POST /simulate ---
        http_api = apigwv2.HttpApi(
            self,
            "SimulatorHttpApi",
            api_name="centralino-simulator",
            description=(
                "Simulatore canale testo (dev): POST /simulate invoca la Lambda "
                "motore-conversazionale con contratto testo-in/testo-out, senza "
                "numero telefonico reale e senza costi vocali."
            ),
        )
        http_api.add_routes(
            path=_SIMULATE_ROUTE,
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "SimulateIntegration", handler=motore_fn
            ),
        )

        # Esposti come attributi per riuso/test (assertions IaC, task future).
        self.motore_function = motore_fn
        self.simulator_api = http_api

    def _make_workflow_lambda(
        self, construct_id: str, function_name: str, handler: str
    ) -> lambda_.Function:
        """Crea una Lambda thin della state machine con impacchettamento condiviso.

        Tutte le Lambda della confirmation-workflow (notificatore-telegram,
        scrivi-calendario, gestore-consenso, telegram-callback) condividono lo
        stesso codice impacchettato dalla radice del progetto (src/ + lambdas/),
        con le stesse esclusioni della Lambda motore. I permessi least-privilege
        (tabella, SendTaskSuccess) sono concessi dal chiamante in modo mirato.

        Args:
            construct_id: Id del construct nello stack.
            function_name: Nome fisico della Lambda (hyphen, no em dash).
            handler: "modulo.funzione" del thin handler in lambdas/.

        Returns:
            La Lambda creata (senza permessi extra: li concede il chiamante).
        """
        return lambda_.Function(
            self,
            construct_id,
            function_name=function_name,
            runtime=_LAMBDA_RUNTIME,
            handler=handler,
            code=lambda_.Code.from_asset(_PROJECT_ROOT, exclude=_ASSET_EXCLUDE),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={
                _ENV_PAID_COMPONENTS: str(self.paid_components_enabled).lower(),
                _ENV_TABLE_NAME: self.table.table_name,
            },
        )

    def _create_confirmation_workflow(self) -> None:
        """Crea la state machine Step Functions confirmation-workflow (Task 11).

        design.md - Decisione 4. Modella la Finestra_Conferma di 8 ore come un
        workflow di lunga durata in attesa di un evento esterno:

          Provvisorio (Pass)
            -> AttendiConferma: invoca notificatore-telegram con waitForTaskToken
               e TIMEOUT ESPLICITO di 8h sul task (non HeartbeatSeconds). Il
               taskToken viaggia nel messaggio Telegram; la Lambda telegram-callback
               fara' SendTaskSuccess quando una dottoressa preme un bottone.
               - callback ricevuto -> il task ritorna {action: conferma|blocker}
               - timeout 8h (nessuna azione) -> errore States.Timeout catturato,
                 instrada a ConfermatoAuto (Requirement 6.4)
            -> EsitoAzione (Choice): blocker -> DaRivedere (end, rescheduling out
               of scope, Requirement 6.5); altrimenti -> ScritturaCalendario
            (ConfermatoAuto confluisce anch'esso in ScritturaCalendario)
            -> ScritturaCalendario: invoca scrivi-calendario con Retry (max 3,
               intervallo >= 2s, Requirement 7.6); su fallimento persistente
               Catch -> CalendarioFallito (Requirement 7.7)
            -> InviaConsenso: invoca gestore-consenso (best-effort, non declassa)
            -> Completato (Succeed)

        La MUTUA ESCLUSIONE tra Conferma e Blocker (Requirement 6.6) e' garantita a
        livello di dato dalla Lambda telegram-callback (conditional write su
        DynamoDB da PROVVISORIO): solo la prima azione sblocca la macchina.

        Least privilege / sicurezza:
          - la state machine puo' invocare SOLO le tre Lambda-task (grant di
            LambdaInvoke sugli ARN specifici tramite i task);
          - la Lambda telegram-callback riceve grant SendTaskSuccess sulla sola
            state machine (nessun wildcard);
          - logging su CloudWatch Logs abilitato (LogLevel ALL) e X-Ray tracing;
          - tutte le risorse ereditano i tag obbligatori da Tags.of(self).

        Costo-zero: Standard Workflow (free tier 4000 transizioni/mese); qui si
        DEFINISCE solo la macchina, nessun deploy/esecuzione.
        """
        # --- Lambda della workflow (thin handler condiviso) ---
        notificatore_fn = self._make_workflow_lambda(
            "NotificatoreTelegram",
            "centralino-notificatore-telegram",
            _NOTIFICATORE_HANDLER,
        )
        scrivi_calendario_fn = self._make_workflow_lambda(
            "ScriviCalendario",
            "centralino-scrivi-calendario",
            _SCRIVI_CALENDARIO_HANDLER,
        )
        gestore_consenso_fn = self._make_workflow_lambda(
            "GestoreConsenso",
            "centralino-gestore-consenso",
            _GESTORE_CONSENSO_HANDLER,
        )

        # --- Wiring servizi esterni: Google (scrivi-calendario) + Telegram ---
        # (notificatore + callback). Env con NOME del secret e valori NON segreti
        # (calendarId, chat_id); grant GetSecretValue least-privilege sul solo
        # secret pertinente. I valori dei secret li popola Alberto dopo il deploy.
        import json as _json

        # scrivi-calendario: layer Google condiviso (creato nel costruttore) +
        # chiave Google + mapping calendarId.
        scrivi_calendario_fn.add_layers(self.google_layer)
        scrivi_calendario_fn.add_environment(
            _ENV_GOOGLE_SA_SECRET_NAME, self.google_sa_secret.secret_name
        )
        scrivi_calendario_fn.add_environment(
            _ENV_GOOGLE_CALENDAR_IDS, _json.dumps(_GOOGLE_CALENDAR_IDS)
        )
        self.google_sa_secret.grant_read(scrivi_calendario_fn)

        # notificatore-telegram: token bot + chat_id del gruppo.
        notificatore_fn.add_environment(
            _ENV_TELEGRAM_BOT_TOKEN_SECRET_NAME, self.telegram_bot_secret.secret_name
        )
        notificatore_fn.add_environment(
            _ENV_TELEGRAM_CHAT_ID, self.telegram_chat_id
        )
        self.telegram_bot_secret.grant_read(notificatore_fn)
        # Il notificatore salva il taskToken sull'item APPT (per il callback):
        # env col nome tabella + grant di scrittura least-privilege.
        notificatore_fn.add_environment(_ENV_TABLE_NAME, self.table.table_name)
        self.table.grant_read_write_data(notificatore_fn)

        # telegram-callback: webhook secret per validare l'autenticita' dei callback.
        telegram_callback_env_secret = self.telegram_webhook_secret
        # --- Bucket S3 del PDF di consenso (Task 12/18, Requirement 9.6) ---
        # Encryption at rest attiva (SSE S3-managed, nessun costo KMS), accesso
        # pubblico bloccato, TLS forzato. RemovalPolicy DESTROY + auto_delete
        # cosi' `cdk destroy` non lascia risorse orfane (Requirement 9.3).
        consenso_bucket = s3.Bucket(
            self,
            "ConsensoPdfBucket",
            encryption=s3.BucketEncryption.S3_MANAGED,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        # Least privilege: il gestore-consenso legge SOLO l'oggetto PDF (non
        # l'intero bucket). grant_read con il prefisso dell'oggetto limita
        # s3:GetObject all'ARN specifico dell'oggetto (Requirement 9.4).
        consenso_bucket.grant_read(gestore_consenso_fn, _CONSENSO_PDF_KEY)
        # Least privilege SES: ses:SendRawEmail sull'identita' del mittente E su
        # quelle dei destinatari di test. In sandbox SES verifica il permesso
        # anche sull'identita' del destinatario (ecco perche' le includiamo).
        # Nessun wildcard sulle risorse (Requirement 9.4): ARN espliciti per
        # ciascuna identita'. In produzione (fuori sandbox) basterebbe il mittente.
        _ses_identities = [_CONSENSO_SENDER_EMAIL, *_CONSENSO_TEST_RECIPIENTS]
        gestore_consenso_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ses:SendRawEmail"],
                resources=[
                    self.format_arn(
                        service="ses",
                        resource="identity",
                        resource_name=identity,
                    )
                    for identity in dict.fromkeys(_ses_identities)  # dedup, ordine
                ],
            )
        )
        # Env di configurazione del gestore-consenso (nessun segreto): bucket,
        # chiave PDF e mittente verificato. Allineate a lambdas/gestore_consenso.py.
        gestore_consenso_fn.add_environment(
            _ENV_CONSENSO_PDF_BUCKET, consenso_bucket.bucket_name
        )
        gestore_consenso_fn.add_environment(
            _ENV_CONSENSO_PDF_KEY, _CONSENSO_PDF_KEY
        )
        gestore_consenso_fn.add_environment(
            _ENV_CONSENSO_SENDER_EMAIL, _CONSENSO_SENDER_EMAIL
        )
        # telegram-callback: legge/scrive la tabella (conditional write) e
        # sblocca la state machine via SendTaskSuccess (grant piu' sotto).
        telegram_callback_fn = self._make_workflow_lambda(
            "TelegramCallback",
            "centralino-telegram-callback",
            _TELEGRAM_CALLBACK_HANDLER,
        )
        # Least privilege sui dati: solo chi tocca la tabella riceve il grant.
        self.table.grant_read_write_data(telegram_callback_fn)
        # notificatore/scrivi-calendario/gestore-consenso non scrivono la tabella
        # single-table direttamente in questo flusso: nessun grant tabella (il
        # cambio di stato lo fa telegram-callback / la state machine annota l'esito).

        # --- Stati della macchina (design.md - Decisione 4) ---
        # Provvisorio: punto d'ingresso, annota lo stato di partenza.
        provvisorio = sfn.Pass(
            self,
            "Provvisorio",
            comment="Appuntamento registrato in Stato_Provvisorio (Requirement 5.5).",
            result=sfn.Result.from_object({"status": "PROVVISORIO"}),
            result_path="$.provvisorio",
        )

        # AttendiConferma: invia recap Telegram con taskToken (waitForTaskToken) e
        # timeout esplicito di 8h. Il payload include il TaskToken cosi' la Lambda
        # telegram-callback puo' fare SendTaskSuccess (design.md - Decisione 4).
        attendi_conferma = sfn_tasks.LambdaInvoke(
            self,
            "AttendiConferma",
            lambda_function=notificatore_fn,
            integration_pattern=sfn.IntegrationPattern.WAIT_FOR_TASK_TOKEN,
            # Timeout ESPLICITO sul task di callback (NON HeartbeatSeconds):
            # allo scadere delle 8h Step Functions solleva States.Timeout, che
            # catturiamo per instradare a ConfermatoAuto (Requirement 6.4).
            task_timeout=sfn.Timeout.duration(_CONFIRMATION_WINDOW),
            payload=sfn.TaskInput.from_object(
                {
                    "taskToken": sfn.JsonPath.task_token,
                    "appointment.$": "$.appointment",
                }
            ),
            # L'esito del callback (action) finisce sotto $.callback.
            result_path="$.callback",
        )

        # ConfermatoAuto: timeout 8h senza azioni -> conferma automatica (6.4).
        confermato_auto = sfn.Pass(
            self,
            "ConfermatoAuto",
            comment="Timeout 8h senza azioni: conferma automatica (Requirement 6.4).",
            result=sfn.Result.from_object({"status": _STATUS_CONFERMATO_AUTO}),
            result_path="$.esito",
        )

        # DaRivedere: azione blocker entro 8h -> da rivedere (6.5). Rescheduling
        # out of scope: stato terminale per questa fase.
        da_rivedere = sfn.Pass(
            self,
            "DaRivedere",
            comment="Blocker entro 8h: appuntamento da rivedere (Requirement 6.5).",
            result=sfn.Result.from_object({"status": _STATUS_DA_RIVEDERE}),
            result_path="$.esito",
        )
        da_rivedere_end = sfn.Succeed(
            self,
            "DaRivedereFine",
            comment="Fine: rescheduling out of scope (Requirement 6.5).",
        )
        da_rivedere.next(da_rivedere_end)

        # ScritturaCalendario: crea l'evento definitivo, Retry 3 x >= 2s (7.6).
        scrittura_calendario = sfn_tasks.LambdaInvoke(
            self,
            "ScritturaCalendario",
            lambda_function=scrivi_calendario_fn,
            payload_response_only=True,
            result_path="$.calendar",
        )
        scrittura_calendario.add_retry(
            errors=["States.ALL"],
            max_attempts=_CALENDAR_MAX_ATTEMPTS,
            interval=_CALENDAR_RETRY_INTERVAL,
            backoff_rate=1.0,  # intervallo costante >= 2s (Requirement 7.6).
        )

        # CalendarioFallito: fallimento persistente dopo 3 retry (7.7).
        calendario_fallito = sfn.Pass(
            self,
            "CalendarioFallito",
            comment="Scrittura calendario fallita dopo 3 tentativi (Requirement 7.7).",
            result=sfn.Result.from_object({"status": _STATUS_CALENDARIO_FALLITO}),
            result_path="$.esito",
        )
        calendario_fallito_end = sfn.Fail(
            self,
            "CalendarioFallitoFine",
            cause="Scrittura su Google Calendar fallita dopo i retry.",
            error="CalendarWriteError",
        )
        calendario_fallito.next(calendario_fallito_end)

        # InviaConsenso: email col PDF di consenso (best-effort, non declassa 8.x).
        invia_consenso = sfn_tasks.LambdaInvoke(
            self,
            "InviaConsenso",
            lambda_function=gestore_consenso_fn,
            payload_response_only=True,
            result_path="$.consenso",
        )
        completato = sfn.Succeed(
            self,
            "Completato",
            comment="Flusso completato: calendario scritto e consenso gestito.",
        )

        # Catena conferma -> scrittura -> consenso -> completato.
        scrittura_calendario.add_catch(
            calendario_fallito, errors=["States.ALL"], result_path="$.error"
        )
        scrittura_calendario.next(invia_consenso)
        invia_consenso.next(completato)

        # EsitoAzione: Choice sul risultato del callback (conferma vs blocker).
        esito_azione = sfn.Choice(
            self,
            "EsitoAzione",
            comment="Prima azione cronologica: conferma -> calendario, blocker -> da rivedere.",
        )
        esito_azione.when(
            sfn.Condition.string_equals("$.callback.action", "blocker"),
            da_rivedere,
        )
        esito_azione.otherwise(scrittura_calendario)

        # ConfermatoAuto confluisce nella scrittura calendario (come la conferma).
        confermato_auto.next(scrittura_calendario)

        # AttendiConferma: su timeout 8h -> ConfermatoAuto; altrimenti EsitoAzione.
        attendi_conferma.add_catch(
            confermato_auto,
            errors=["States.Timeout"],
            result_path="$.timeout",
        )
        attendi_conferma.next(esito_azione)

        # Definizione: Provvisorio -> AttendiConferma -> ...
        definition = provvisorio.next(attendi_conferma)

        # Log group dedicato con retention breve (costo-zero) + encryption di
        # default del servizio Logs (Requirement 9.6).
        log_group = logs.LogGroup(
            self,
            "ConfirmationWorkflowLogs",
            log_group_name="/aws/vendedlogs/states/centralino-confirmation-workflow",
            retention=_SFN_LOG_RETENTION,
            removal_policy=RemovalPolicy.DESTROY,
        )

        state_machine = sfn.StateMachine(
            self,
            "ConfirmationWorkflow",
            state_machine_name="centralino-confirmation-workflow",
            # Standard Workflow: attese lunghe (8h) + free tier 4000 transizioni.
            state_machine_type=sfn.StateMachineType.STANDARD,
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            # Osservabilita' (design.md - Osservabilita' e costi): log completi
            # su CloudWatch Logs + tracing X-Ray.
            logs=sfn.LogOptions(
                destination=log_group,
                level=sfn.LogLevel.ALL,
                include_execution_data=True,
            ),
            tracing_enabled=True,
        )

        # Least privilege: la Lambda telegram-callback puo' sbloccare SOLO questa
        # state machine (SendTaskSuccess/Failure/Heartbeat) - nessun wildcard.
        state_machine.grant_task_response(telegram_callback_fn)
        # Webhook secret per validare l'autenticita' dei callback Telegram.
        telegram_callback_fn.add_environment(
            _ENV_TELEGRAM_WEBHOOK_SECRET_NAME,
            telegram_callback_env_secret.secret_name,
        )
        telegram_callback_env_secret.grant_read(telegram_callback_fn)
        telegram_callback_fn.add_environment(
            "CONFIRMATION_STATE_MACHINE_ARN", state_machine.state_machine_arn
        )

        # Il Motore_Conversazionale avvia la workflow quando registra un APPT
        # provvisorio (design.md - Core -> StartExecution). Grant mirato.
        state_machine.grant_start_execution(self.motore_function)
        self.motore_function.add_environment(
            "CONFIRMATION_STATE_MACHINE_ARN", state_machine.state_machine_arn
        )

        # --- Webhook Telegram: API Gateway /telegram/webhook -> callback Lambda ---
        webhook_api = apigwv2.HttpApi(
            self,
            "TelegramWebhookApi",
            api_name="centralino-telegram-webhook",
            description=(
                "Webhook Telegram: POST /telegram/webhook invoca la Lambda "
                "telegram-callback, che applica la transizione di stato (mutua "
                "esclusione) e sblocca la state machine via SendTaskSuccess."
            ),
        )
        webhook_api.add_routes(
            path=_TELEGRAM_WEBHOOK_ROUTE,
            methods=[apigwv2.HttpMethod.POST],
            integration=apigwv2_integrations.HttpLambdaIntegration(
                "TelegramWebhookIntegration", handler=telegram_callback_fn
            ),
        )

        # Esposti come attributi per riuso/test (assertions IaC, snapshot ASL).
        self.confirmation_state_machine = state_machine
        self.confirmation_log_group = log_group
        self.notificatore_function = notificatore_fn
        self.scrivi_calendario_function = scrivi_calendario_fn
        self.gestore_consenso_function = gestore_consenso_fn
        self.telegram_callback_function = telegram_callback_fn
        self.telegram_webhook_api = webhook_api
        self.consenso_bucket = consenso_bucket

    def _create_lex_voice_layer(self) -> None:
        """Crea il bot Lex V2 come strato voce (Task 16) - SOLO se a pagamento.

        Chiamata solo quando self.paid_components_enabled e' True (Requirement
        10.2): con il default la synth non produce alcuna risorsa Lex, cosi' il
        progetto resta a costo zero (Requirement 10.1).

        Design (design.md - Decisione 1, Opzione B; Centralino_Vocale):
        Lex e' usato come SOLO strato voce (ASR italiano + TTS/Polly), non come
        cervello. Un unico intent "catch-all" inoltra ogni utterance trascritta
        alla Lambda `motore-conversazionale`, che possiede lo stato del dialogo e
        invoca Bedrock. In Lex V2 il catch-all naturale e' la FallbackIntent
        (AMAZON.FallbackIntent): cattura tutto cio' che non e' un intent
        specifico. Poiche' non definiamo altri intent, ogni utterance cade li'.

        Il fulfillment code hook della FallbackIntent e' abilitato; l'associazione
        alla Lambda concreta avviene a livello di alias del bot
        (BotAliasLocaleSettings -> LambdaCodeHook con l'ARN della Lambda motore),
        come richiede Lex V2. Concediamo a lex.amazonaws.com il permesso di
        invocare la Lambda (Requirement 1.2, 2.1).
        """
        # Ruolo IAM che Lex assume a runtime. La managed policy dedicata a Lex V2
        # (AmazonLexRunBotsOnly) concede i permessi minimi per l'esecuzione del
        # bot. L'invocazione della Lambda e' concessa a parte via Lambda
        # permission (vedi sotto), coerente col least privilege (Requirement 9.4).
        lex_role = iam.Role(
            self,
            "LexBotRole",
            role_name="centralino-lex-bot-role",
            assumed_by=iam.ServicePrincipal("lexv2.amazonaws.com"),
            description=(
                "Ruolo di esecuzione del bot Lex V2 (strato voce Idelia). "
                "Least privilege: esecuzione del bot; l'invoke della Lambda "
                "motore e' concesso via Lambda permission dedicata."
            ),
        )
        # Permesso a Lex di sintetizzare la voce con Polly (TTS del bot).
        lex_role.add_to_policy(
            iam.PolicyStatement(
                actions=["polly:SynthesizeSpeech"],
                resources=["*"],
            )
        )

        # Intent catch-all = FallbackIntent con fulfillment code hook abilitato.
        # Ogni utterance non mappata (cioe' tutte, qui) viene inoltrata alla
        # Lambda tramite il code hook associato a livello di alias.
        fallback_intent = lex.CfnBot.IntentProperty(
            name=_LEX_FALLBACK_INTENT,
            description=(
                "Intent catch-all: inoltra ogni utterance trascritta alla "
                "Lambda motore-conversazionale (Requirement 1.2, 2.1)."
            ),
            parent_intent_signature=_LEX_FALLBACK_SIGNATURE,
            fulfillment_code_hook=lex.CfnBot.FulfillmentCodeHookSettingProperty(
                enabled=True,
            ),
        )

        # Lex richiede che il locale abbia ALMENO UN intent normale (con sample
        # utterances) oltre alla FallbackIntent, altrimenti il build del locale
        # fallisce ("locale doesn't contain a fallback intent"). Definiamo un
        # intent "conversazione" con qualche frase-esempio in italiano: anche
        # questo ha il fulfillment code hook verso la Lambda, cosi' sia le frasi
        # riconosciute sia tutto il resto (via FallbackIntent) finiscono al motore.
        conversazione_intent = lex.CfnBot.IntentProperty(
            name="Conversazione",
            description=(
                "Intent conversazionale: inoltra alla Lambda motore. Presente "
                "per soddisfare il requisito Lex di un intent normale nel locale."
            ),
            sample_utterances=[
                lex.CfnBot.SampleUtteranceProperty(utterance="vorrei un appuntamento"),
                lex.CfnBot.SampleUtteranceProperty(utterance="buongiorno"),
                lex.CfnBot.SampleUtteranceProperty(utterance="ho bisogno di aiuto"),
                lex.CfnBot.SampleUtteranceProperty(utterance="vorrei prenotare un colloquio"),
                lex.CfnBot.SampleUtteranceProperty(utterance="informazioni"),
            ],
            fulfillment_code_hook=lex.CfnBot.FulfillmentCodeHookSettingProperty(
                enabled=True,
            ),
        )

        # Locale italiano: intent conversazione + FallbackIntent (catch-all).
        it_locale = lex.CfnBot.BotLocaleProperty(
            locale_id=_LEX_LOCALE_IT,
            nlu_confidence_threshold=_LEX_NLU_THRESHOLD,
            voice_settings=lex.CfnBot.VoiceSettingsProperty(voice_id=_LEX_VOICE_ID),
            intents=[conversazione_intent, fallback_intent],
        )

        # Il bot Lex V2. auto_build_bot_locales=True fa costruire il locale al
        # deploy (necessario perche' l'alias possa referenziare una versione).
        bot = lex.CfnBot(
            self,
            "CentralinoLexBot",
            name="centralino-voce-idelia",
            description=(
                "Strato voce Idelia: bot Lex V2 in italiano con intent "
                "catch-all che inoltra le utterance alla Lambda motore."
            ),
            role_arn=lex_role.role_arn,
            # data_privacy e' tipizzato Any in CFN e vuole la chiave PascalCase
            # ChildDirected: passiamo il dict raw per evitare il mismatch di
            # naming (dati finti, non rivolto a minori come target COPPA).
            data_privacy={"ChildDirected": False},
            idle_session_ttl_in_seconds=_LEX_SESSION_TTL_S,
            auto_build_bot_locales=True,
            bot_locales=[it_locale],
        )

        # Alias del bot: qui si associa concretamente la Lambda al code hook del
        # locale (LambdaCodeHook). E' il punto dove Lex "wire-a" il catch-all
        # alla Lambda motore-conversazionale.
        bot_alias = lex.CfnBotAlias(
            self,
            "CentralinoLexBotAlias",
            bot_alias_name="live",
            bot_id=bot.attr_id,
            bot_alias_locale_settings=[
                lex.CfnBotAlias.BotAliasLocaleSettingsItemProperty(
                    locale_id=_LEX_LOCALE_IT,
                    bot_alias_locale_setting=lex.CfnBotAlias.BotAliasLocaleSettingsProperty(
                        enabled=True,
                        code_hook_specification=lex.CfnBotAlias.CodeHookSpecificationProperty(
                            lambda_code_hook=lex.CfnBotAlias.LambdaCodeHookProperty(
                                # Versione del contratto del message di Lex verso
                                # la Lambda (1.0 e' quella corrente per Lex V2).
                                code_hook_interface_version="1.0",
                                lambda_arn=self.motore_function.function_arn,
                            )
                        ),
                    ),
                )
            ],
        )

        # Least privilege: Lex puo' invocare SOLO la Lambda motore (Requirement
        # 1.2, 2.1, 9.4). Nessun wildcard: la permission e' sull'ARN specifico.
        self.motore_function.add_permission(
            "AllowLexInvoke",
            principal=iam.ServicePrincipal("lexv2.amazonaws.com"),
            action="lambda:InvokeFunction",
        )

        # Esposti come attributi per test/riuso (assertions IaC, Task 17 Connect).
        self.lex_role = lex_role
        self.lex_bot = bot
        self.lex_bot_alias = bot_alias

    def _build_contact_flow_content(self, lex_bot_alias_arn: str) -> str:
        """Costruisce il contenuto JSON del contact flow inbound (Requirement 1).

        Il contact flow inbound di Amazon Connect e' descritto da un documento
        JSON di "action block". Modelliamo il flusso richiesto (Requirement 1):

          1. Benvenuto vocale in italiano (MessageParticipant, TTS/Polly),
             riprodotto all'ingresso della chiamata (criterio 1.1).
          2. Raccolta dell'input del paziente delegata al bot Lex V2 (Task 16):
             il blocco ConnectParticipantWithLexBot fa ASR italiano + barge-in e
             inoltra l'utterance trascritta alla Lambda motore (criteri 1.2, 1.3).
          3. Su silenzio (nessun input entro il timeout) si riproduce un prompt
             di ri-tentativo, ripetuto fino a un massimo di 3 volte, poi si chiude
             la chiamata con un messaggio di chiusura (criterio 1.4).
          4. Su input non riconosciuto / bassa confidenza si riproduce un
             messaggio che invita a ripetere, mantenendo attiva la chiamata
             (criterio 1.5).

        Nota: il documento e' volutamente descrittivo/dichiarativo. La logica di
        controllo (limite ri-prompt, bassa confidenza, soglie temporali) e'
        modellata come funzioni pure testabili in src/centralino_vocale.py ed e'
        verificata dai property test (Task 17.1) a costo zero, dato che il vero
        contact flow gira solo con Connect attivo (a pagamento).

        Args:
            lex_bot_alias_arn: ARN dell'alias del bot Lex a cui delegare l'ASR.

        Returns:
            Stringa JSON serializzata pronta per CfnContactFlow.content.
        """
        content = {
            "Version": "2019-10-30",
            "StartAction": "welcome",
            "Metadata": {
                "entryPointPosition": {"x": 0, "y": 0},
                "description": (
                    "Idelia inbound: benvenuto IT, ASR/TTS via Lex, ri-prompt "
                    "max 3, gestione bassa confidenza (Requirement 1)."
                ),
            },
            "Actions": [
                {
                    # 1) Benvenuto vocale in italiano (Requirement 1.1).
                    "Identifier": "welcome",
                    "Type": "MessageParticipant",
                    "Parameters": {"Text": _CONNECT_WELCOME_IT},
                    "Transitions": {"NextAction": "getCustomerInput"},
                },
                {
                    # 2) Raccolta input via bot Lex (ASR IT + barge-in), che
                    # inoltra l'utterance alla Lambda motore (Requirement 1.2/1.3).
                    "Identifier": "getCustomerInput",
                    "Type": "ConnectParticipantWithLexBot",
                    "Parameters": {
                        "LexV2Bot": {"AliasArn": lex_bot_alias_arn},
                        # Barge-in: il paziente puo' interrompere il prompt.
                        "BargeInEnabled": "true",
                        # Timeout di attesa input (Requirement 1.4: 10s).
                        "InputTimeLimitSeconds": str(_CONNECT_INPUT_TIMEOUT_S),
                    },
                    "Transitions": {
                        # Input riconosciuto: prosegui (l'esito reale e' gestito
                        # dalla Lambda motore; qui il flusso continua/termina).
                        "NextAction": "disconnect",
                        # Nessun input (silenzio) -> ramo ri-prompt (1.4).
                        "Conditions": [],
                        "Errors": [
                            {
                                "ErrorType": "NoMatchingCondition",
                                "NextAction": "reprompt",
                            },
                            {
                                # Bassa confidenza / non riconosciuto (1.5).
                                "ErrorType": "NoMatchingError",
                                "NextAction": "lowConfidence",
                            },
                        ],
                    },
                },
                {
                    # 3) Ri-prompt su silenzio (Requirement 1.4). Il limite di 3
                    # tentativi e la chiusura sono modellati/verificati in
                    # src/centralino_vocale.py (plan_after_silence) e dai test.
                    "Identifier": "reprompt",
                    "Type": "MessageParticipant",
                    "Parameters": {
                        "Text": _CONNECT_REPROMPT_IT,
                        # Numero massimo di ri-tentativi prima della chiusura.
                        "MaxReprompts": str(_CONNECT_MAX_REPROMPTS),
                    },
                    "Transitions": {
                        "NextAction": "getCustomerInput",
                        "Errors": [
                            {
                                # Ri-prompt esauriti -> messaggio di chiusura.
                                "ErrorType": "NoMatchingError",
                                "NextAction": "goodbye",
                            }
                        ],
                    },
                },
                {
                    # 4) Bassa confidenza: invita a ripetere, chiamata ATTIVA
                    # (Requirement 1.5) -> torna a raccogliere l'input.
                    "Identifier": "lowConfidence",
                    "Type": "MessageParticipant",
                    "Parameters": {"Text": _CONNECT_LOW_CONFIDENCE_IT},
                    "Transitions": {"NextAction": "getCustomerInput"},
                },
                {
                    # Messaggio di chiusura dopo i ri-prompt esauriti (1.4).
                    "Identifier": "goodbye",
                    "Type": "MessageParticipant",
                    "Parameters": {"Text": _CONNECT_GOODBYE_IT},
                    "Transitions": {"NextAction": "disconnect"},
                },
                {
                    # Disconnessione: fine chiamata.
                    "Identifier": "disconnect",
                    "Type": "DisconnectParticipant",
                    "Parameters": {},
                    "Transitions": {},
                },
            ],
        }
        return json.dumps(content)

    def _create_connect_voice_layer(self) -> None:
        """Crea l'istanza Amazon Connect e il contact flow inbound (Task 17).

        Chiamata solo quando self.paid_components_enabled e' True (Requirement
        10.2): con il default la synth non produce alcuna risorsa Connect, cosi'
        il progetto resta a costo zero (Requirement 10.1). Amazon Connect e' a
        pagamento (numero telefonico + tariffa al minuto).

        Design (design.md - Decisione 1, Opzione B; Centralino_Vocale):
        Connect e' lo strato telefonico. Il contact flow inbound riproduce il
        benvenuto italiano, gestisce ASR/TTS (Polly) e barge-in delegando la
        comprensione al bot Lex V2 (Task 16), applica i ri-prompt su silenzio
        (max 3) e la gestione della bassa confidenza (Requirement 1.1-1.5). Il
        bot Lex a sua volta inoltra l'utterance alla Lambda motore.

        Risorse create (tutte gated dal flag):
          - CfnInstance: istanza Connect con inbound calls abilitato.
          - CfnIntegrationAssociation: associa il bot Lex V2 all'istanza (LEX_BOT)
            cosi' il contact flow puo' referenziarlo.
          - CfnContactFlow: il flusso inbound (CONTACT_FLOW) col contenuto JSON.
        """
        # Istanza Connect. Storage identity gestita internamente (CONNECT_MANAGED):
        # nessuna dipendenza da directory esterne. Inbound abilitato (1.1).
        instance = connect.CfnInstance(
            self,
            "ConnectInstance",
            identity_management_type="CONNECT_MANAGED",
            instance_alias=_CONNECT_INSTANCE_ALIAS,
            attributes=connect.CfnInstance.AttributesProperty(
                inbound_calls=True,
                outbound_calls=False,
            ),
        )

        # Associazione del bot Lex V2 all'istanza Connect: rende il bot
        # referenziabile dal contact flow (ASR italiano + barge-in via Lex).
        # Richiede l'ARN dell'alias del bot creato in _create_lex_voice_layer.
        lex_association = connect.CfnIntegrationAssociation(
            self,
            "ConnectLexAssociation",
            instance_id=instance.attr_arn,
            integration_type="LEX_BOT",
            integration_arn=self.lex_bot_alias.attr_arn,
        )

        # Contact flow inbound col contenuto JSON (benvenuto, Lex, ri-prompt,
        # bassa confidenza). Type CONTACT_FLOW = flusso di contatto entrante.
        contact_flow = connect.CfnContactFlow(
            self,
            "ConnectInboundFlow",
            instance_arn=instance.attr_arn,
            name="centralino-idelia-inbound",
            type="CONTACT_FLOW",
            description=(
                "Contact flow inbound Idelia: benvenuto italiano, ASR/TTS via "
                "Lex, barge-in, ri-prompt max 3 e gestione bassa confidenza "
                "(Requirement 1.1-1.5)."
            ),
            content=self._build_contact_flow_content(self.lex_bot_alias.attr_arn),
        )
        # Il contact flow referenzia il bot: l'associazione va creata prima.
        # Dipendenza a livello di construct (node) per l'ordine di creazione.
        contact_flow.node.add_dependency(lex_association)

        # Esposti come attributi per test/riuso (assertions IaC).
        self.connect_instance = instance
        self.connect_lex_association = lex_association
        self.connect_contact_flow = contact_flow
