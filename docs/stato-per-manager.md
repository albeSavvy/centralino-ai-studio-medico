# Idelia — Centralino AI per studio di psicologia — Stato progetto

## In una frase

Ho progettato e realizzato su AWS un centralino intelligente per uno studio di
psicologia: riceve una richiesta di appuntamento, dialoga in italiano con l'utente,
raccoglie i dati, fissa il colloquio sul calendario delle dottoresse e le avvisa
per la conferma. Tutto come codice (Infrastructure as Code), riproducibile e
testato.

## Fatto e VERIFICATO end-to-end su AWS

- ✅ **Dialogo AI completo** (Amazon Bedrock, Claude Haiku 4.5): Idelia conduce la
  conversazione in linguaggio naturale, capisce l'intento e raccoglie i dati.
- ✅ **Multi-turno robusto**: il dialogo accumula i dati tra un turno e l'altro
  (nome, età, telefono, motivo, sede, data) e non richiede più le stesse
  informazioni. Verificato su DynamoDB.
- ✅ **Telefonia reale** (Amazon Connect + Lex V2): Idelia risponde a un numero
  telefonico con voce italiana, capisce il parlato e dialoga. Provato con chiamata
  vera.
- ✅ **Flusso end-to-end orchestrato** (Step Functions): raccolta dati → assegna
  colloquio → notifica Telegram → conferma con bottone → scrittura calendario →
  email di consenso. Eseguito dal vivo, state machine conclusa con esito SUCCEEDED.
- ✅ **Notifica Telegram reale**: recap dell'appuntamento (paziente, data/ora,
  dottoressa) con pulsanti "Conferma" / "Segnala blocker" nel gruppo dottoresse.
  Il campo "problema" NON è incluso nel recap (privacy).
- ✅ **Scrittura reale su Google Calendar**: evento del colloquio creato sul
  calendario della dottoressa.
- ✅ **Email di consenso** (Amazon SES) inviata su conferma.
- ✅ **Infrastruttura completa** deployata via AWS CDK in Python: nessuna risorsa
  creata a mano (eccetto lo strato voce Connect/Lex, che è console-first).
- ✅ **Qualità**: 190 test automatici tutti verdi (property-based + unit +
  contract); template infrastruttura validato.
- ✅ **Sicurezza / buone pratiche**: credenziali in AWS Secrets Manager (mai nel
  codice), permessi IAM minimi per componente, cifratura at rest, billing alarm a
  1 USD. Allineato ai pilastri AWS Well-Architected.

## Componenti e servizi AWS usati

Serverless ed event-driven: AWS Lambda, Amazon DynamoDB, AWS Step Functions,
Amazon API Gateway, Amazon SES, AWS Secrets Manager, Amazon S3, Amazon CloudWatch,
AWS Budgets. Dialogo AI: Amazon Bedrock. Voce/telefonia: Amazon Connect + Amazon
Lex V2. Integrazioni esterne: Google Calendar, Telegram.

## Conformità AI (già valutata)

- Classificato **rischio limitato** ai sensi dell'EU AI Act: Idelia instrada e
  prenota, NON dà consigli clinici né analizza le emozioni. L'unico obbligo che lo
  tocca è la **trasparenza (Art. 50)**: dichiarare che è un assistente AI.
- Dettaglio in `docs/ai-act-compliance.md`.

## Prossimi step

- ⏳ **Disclaimer AI** nel messaggio di apertura (unica azione di compliance
  aperta, Art. 50).
- ⏳ **Portfolio**: pubblicazione su GitHub (albeSavvy) e post LinkedIn, con la
  narrativa "operational → business logic".
- ⏳ (Iterazione voce) loop multi-turno nel contact flow di Connect.

## Valore per il mio percorso AWS Solutions Architect

- Progetto reale end-to-end su AWS, non un tutorial: architettura serverless,
  IaC, integrazioni con servizi esterni, sicurezza e controllo costi.
- Decisioni architetturali documentate con trade-off (es. Lambda→Bedrock invece
  di "Lex intelligente" per libertà semantica e controllo latenza).
- Narrativa di evoluzione: da automazione operativa (rispondere al telefono) a
  logica di business con stati (conferme, blocker, finestra 8h).

## Note di trasparenza

- Dati finti (nessun dato reale di pazienti).
- Costo mantenuto vicino a zero: componenti a pagamento attivati solo per i test,
  billing alarm attivo, infrastruttura distrutta a fine sessione (`cdk destroy`),
  numero telefonico rilasciato.
- Per l'uso reale su pazienti servirebbero: valutazione GDPR (dati sanitari e
  minori) e disclosure "assistente AI". Già identificati.
