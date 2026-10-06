# Project Lifecycle — Idelia (Centralino AI Studio di Psicologia)

> Scheda di avanzamento del ciclo di vita del progetto. La aggiorna Kiro quando
> una fase cambia stato. Riferimento alle fasi: steering `project-checklist.md`.

**Progetto AI?** sì (Amazon Bedrock, dialogo semantico)
**Prossima fase da fare:** 4 — GitHub
**Ultimo aggiornamento:** 2026-10-02

Legenda stato: ✅ fatto · 🔄 in corso · ⏳ da fare · ⏭️ saltata (non applicabile)

| Fase | Nome | Stato | Note / link |
|------|------|-------|-------------|
| 0 | Compliance AI | ✅ | Rischio limitato (EU AI Act). `docs/ai-act-compliance.md`. Azione aperta: disclaimer AI in apertura (Art. 50) |
| 1 | Costruire (stato presentabile) | ✅ | Completo e verificato end-to-end (telefono + chat): dialogo → prenotazione → Telegram → Calendar → SES. 190 test verdi |
| 2 | Slide HTML di presentazione | ✅ | `docs/presentazione.html` (deck 6 slide) |
| 3 | Diagramma Mermaid | ✅ | Diagramma Mermaid di Idelia prodotto (freccia bidirezionale Motore↔Bedrock). Anche `docs/architettura.html` (diagramma dettagliato con "Perché") |
| 4 | GitHub (repo su albeSavvy) | ⏳ | Da fare. Prima: verificare `.gitignore` (SA json, layer, .env già esclusi) |
| 5 | LinkedIn (post + link repo) | ⏳ | Dopo la fase 4. Taglio "operational → business logic" |
| 6 | Chiusura (teardown costi + registro) | 🔄 | Teardown fatto più volte dopo i test (numero rilasciato, cdk destroy). Registro `aws-projects.md` aggiornato. DA FARE a fine giornata: rilasciare numero demo + revocare token Telegram (passato in chiaro) |

## Note di avanzamento

- **Fix dialogo (02/10):** un dato alla volta, niente ripetizioni, data odierna
  auto dal sistema, chiusura con `assegna_colloquio`. Verificato chat + telefono.
- **Limite residuo noto:** l'ASR vocale (Lex) può storpiare nomi/numeri al
  telefono; il modello Haiku ogni tanto esita ma recupera. Candidato next: loop
  multi-turno nel contact flow + eventuale modello più capace (Sonnet).
- **Decisioni/trade-off da mettere nelle slide/README:** Lambda→Bedrock vs "Lex
  intelligente"; DynamoDB vs RDS; Step Functions per l'attesa 8h.
- **Presentazione mentor (ott 2026):** creato `docs/decisioni-architetturali.md`
  (perché servizio-per-servizio + 6 trade-off + narrativa operational→business),
  a corredo di `docs/presentazione.html` e del diagramma Mermaid nel README.
