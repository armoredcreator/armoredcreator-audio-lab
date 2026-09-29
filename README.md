# ArmoredCreator — Estado Final de Certificação

> Laboratório de reconstrução e certificação: `armoredcreator/armoredcreator-test`.
> O repositório oficial `armoredcreator/armoredcreator` e a branch `audit/baseline-2026-09-19` permanecem intocados.

## 1. Estado atual — 29/09/2026

Esta é a referência operacional da versão atual.

### E2E real mais recente

Dois itens reais foram processados sem inserção manual de vídeo:

```text
1383 → Vision V1 + Caption → Studio/RVC → Hub → Telegram CONFIRMED #922 → PUBLISHED → cleanup
706  → Vision V1 + Caption → Studio/RVC → Hub → Telegram CONFIRMED #923 → PUBLISHED → cleanup
```

O item 706 é especialmente relevante porque havia falhado historicamente na validação da Caption. Na execução atual ele passou pela Vision, Studio, RVC, Hub, confirmação Telegram e cleanup.

### Caption V1

A Caption pertence à Vision V1 e não identifica nem substitui o produto.

```text
ARMORED_CAPTION_ENABLED=1
ARMORED_CAPTION_MODEL=gemini-3.1-flash-lite
ARMORED_CAPTION_API_TIMEOUT=90
ARMORED_CAPTION_ALLOW_DETERMINISTIC_FALLBACK=0
```

Regras atuais: português do Brasil; 2–3 palavras no texto principal; exatamente 1 emoji; 1–2 hashtags; reação específica ao produto; sem embalagem/tampa/frasco/lacre; sem marca/modelo; sem linguagem comercial; sem reprodução de combinação distintiva do título. Palavras genéricas do tipo do produto podem ser usadas naturalmente.

### Vision V2

**Fora desta versão.** Não existe módulo V2 nem `validate_vision_v2_real.py` nesta branch. Branches antigas de V2 não fazem parte da versão final e não devem ser reintroduzidas.

### Auditoria de certificação — 29/09

A revisão módulo a módulo identificou e corrigiu somente bloqueios comprovados no contrato atual:

- Gemini/API e ausência de credencial agora são tratados como falha técnica RECOVERY; falhas inesperadas de programação/configuração da Caption não são convertidas em WAITING_VISION.
- CATCH-UP não executa Recovery inline durante o scan histórico; ele termina o scan atual e só depois tenta Recovery.
- Quando Recovery realmente progride, o iterador histórico é reconstruído para permitir a retomada no mesmo processo sem perder o checkpoint durável.
- Falha de materialização deixa a reserva RECEIVED sem original e recebe uma única tentativa de rediscovery no mesmo processo; nova falha encerra sem loop.
- PUBLISHED com cleanup_completed=0 participa da Recovery de startup e bloqueia a progressão até o cleanup terminar.
- Migrações SQLite não removem mais tabelas históricas de evidência da Vision.
- Valores reais de origem/grupo/tópico do ambiente não ficam mais como defaults no código ou em .env.example; a configuração operacional deve vir do ambiente/credentials/project.env.

A suíte completa deve ser executada novamente antes de qualquer CATCH-UP histórico real. O CI da branch foi ajustado para executar pytest sobre toda a suíte e tratar RuntimeWarning como erro.

---

## 2. Arquitetura definitiva

Uma única raiz de composição: **Coordinator**.

```text
Telegram fonte
  ↓
ArmoredSync
  ↓
SQLite + storage/videos/{content_id}/
  ↓
Coordinator
  ├─ Vision V1 + Caption
  ├─ Studio + RVC + FFmpeg
  └─ Hub
       ↓
Telegram destino / topic 228
       ↓
confirmação
       ↓
PUBLISHED
       ↓
cleanup
       ↓
próximo item
```

Invariantes: exatamente 1 item ativo; processamento sequencial; SQLite como verdade interna; workspace único por item; sem RabbitMQ/Redis/Celery/Kafka; sem filas físicas; sem pré-download de lote; checkpoint só avança após conclusão; UNKNOWN nunca gera republicação automática.

---

## 3. ArmoredSync

Arquivo principal: `ArmoredSync/service.py`.

- Telegram fonte real.
- Candidato = vídeo + link na mesma mensagem, ou vídeo + link na mensagem imediatamente seguinte.
- Materialização diretamente no workspace canônico.
- CATCH-UP progressivo, um candidato por vez.
- LIVE em rodízio de tópicos, com checkpoints persistidos.
- Não executa Vision, Studio ou Hub.
- Lifecycle Telethon reconstruível; o teste Wi-Fi OFF/ON comprovou reconexão sem o antigo `database is locked`.

**Estado: FECHADO/CERTIFICADO pelos testes automatizados e reais acumulados.**

---

## 4. Core / Coordinator

Arquivos centrais: `models.py`, `database.py`, `storage.py`, `services.py`, `pipeline.py`, `recovery.py`, `startup_audit.py`, `production_contracts.py`, `trace.py`, `coordinator.py`.

- Estados persistidos e publicação `CONFIRMED/ABSENT/UNKNOWN`.
- SQLite, checkpoints, runtime lock e auditoria.
- Pipeline sequencial RECEIVED → VISION → STUDIO → PUBLISHING → PUBLISHED.
- Falhas de processamento entram em RECOVERY em vez de serem abandonadas.
- Recovery reconcilia SQLite + filesystem + Telegram.
- Startup reabre estados legados FAILED através do caminho de recovery.
- Coordinator é a única composição e controla CATCH-UP → LIVE.

**Estado: FECHADO.**

---

## 5. ArmoredVision V1

Arquivos: `ArmoredVision/service.py`, `modules/v1/shopee_api.py`, `modules/v1/shopee_resolver.py`, `modules/v1/caption/generator.py`, `modules/v1/caption/policy.py`.

- Resolve URL Shopee para `shop_id + item_id`.
- Consulta o produto exato pela API de afiliados.
- O link recebido de outra afiliada é somente identidade; não é o link final.
- `offerLink` é preferido quando disponível; fallback usa o `productLink` canônico.
- Caption usa contexto de produto já identificado pela V1.
- Gemini 3.1 Flash-Lite real foi validado.
- Caption válida é requisito para avançar.

**Estado: FECHADO/CERTIFICADO em integração real.**

---

## 6. ArmoredStudio

Arquivos: `ArmoredStudio/service.py`, `unified.py`, `analysis/*`, `processing/*`.

- Studio único e unificado.
- Análise → plano → processamento → finalização.
- Blackbar/banner/VEO/Gemini/exportação fazem parte da camada de análise/plano.
- RVC faz parte do Studio.
- Runtime RVC e voz `melody` foram usados em execução real.
- CPU fallback funcionou.
- Itens 1383 e 706 produziram áudio RVC e vídeo final.
- Finalizer padroniza a saída para Story 1080×1920: escala proporcional + crop central para fontes não-9:16; sem stretch, padding ou blur no vídeo principal.
- O `crop_final` da análise acontece antes da normalização Story.

**Estado: FECHADO; normalização Story coberta por teste de regressão.**

---

## 7. ArmoredHub

Arquivo: `ArmoredHub/service.py`.

- Publicação real no chat `-1004341972306`, topic `228`.
- Persiste intenção/publication e message ID.
- Verifica publicação externamente.
- CONFIRMED → não republica.
- UNKNOWN → recovery; nunca republica cegamente.
- Janela pós-envio protegida por persistência e reconciliação.

Execução real recente: 1383 → `CONFIRMED #922`; 706 → `CONFIRMED #923`.

**Estado: FECHADO/CERTIFICADO.**

---

## 8. Storage e cleanup

Workspace canônico: `storage/videos/{content_id}/`.

SQLite: `storage/database/armoredcreator.db`.

Cleanup só ocorre depois de `PUBLISHED` confirmado. Filas físicas antigas não fazem parte da arquitetura.

**Estado: FECHADO.**

---

## 9. START_ALL

`START_ALL.bat` é o launcher operacional único e chama `run_coordinator.py`, que cria o Coordinator.

Caption V1 e Gemini 3.1 Flash-Lite ficam ativos no launcher real.

**Estado: FECHADO.**

---

## 10. Testes automatizados

Última suíte local limpa registrada:

```text
133 passed, 1 skipped, 8 subtests passed
```

A cobertura inclui arquitetura, invariantes, SQLite, pipeline, recovery, startup audit, runtime lock, Sync, CATCH-UP, restart, publicação, confirmação Telegram, reconnect, LIVE polling, RVC, Vision/Studio e Caption.

**Estado: VERDE no último run local registrado.**

---

## 11. Certificações reais acumuladas

- Telegram fonte real.
- CATCH-UP progressivo.
- Pipeline sequencial.
- Vision V1.
- Caption Gemini dentro da pipeline real.
- Studio/RVC/FFmpeg.
- Hub e Telegram destino.
- Confirmação real.
- Cleanup.
- Restart.
- Recovery.
- Wi-Fi OFF/ON + reconstrução da sessão Telethon.
- Proteção contra republicação.
- Power-loss recovery no item 1383.
- CATCH-UP limitado → LIVE em testes anteriores.

---

## 12. O que falta para declarar certificação histórica 100%

Existe **um único teste operacional de grande escala pendente**:

```text
CATCH-UP histórico completo
≈ 308 conteúdos elegíveis
→ processamento sequencial
→ confirmação
→ cleanup
→ checkpoints
→ entrada automática em LIVE
```

Esse teste não exige inserir vídeo manualmente. O Sync deve descobrir os conteúdos existentes na fonte real.

Um novo conteúdo LIVE não pode ser artificialmente injetado porque a fonte pertence a terceiros. Isso é limitação do laboratório, não lacuna da arquitetura.

---

## 13. Critérios para fechar o histórico

- todos os candidatos elegíveis percorridos;
- nenhum bloqueio sem motivo registrado;
- checkpoints persistidos;
- publicações confirmadas registradas;
- cleanup correto;
- zero órfãos;
- zero filas físicas;
- zero republicações indevidas;
- histórico concluído;
- Coordinator em LIVE.

---

## 14. Congelamento

A branch permanece em fase de certificação. As correções da auditoria de 29/09 são mínimas e estão aguardando a nova execução da suíte automatizada antes do CATCH-UP histórico.

Base da versão congelada antes da correção Story:
```text
a345f2fd997d841e1e38f8e4609ed4af7083dd30
```

Correção Story integrada na branch congelada pelo PR #29. O commit atual da branch é registrado pelo Git e deve ser usado no CATCH-UP após a validação local.

Depois da nova suíte verde e do CATCH-UP histórico, congelar novamente. Até lá, somente falhas bloqueadoras reproduzidas na certificação justificam novas alterações.

Depois do congelamento:

- não alterar Vision V1;
- não alterar Caption;
- não alterar Sync;
- não alterar Studio;
- não alterar Hub;
- não alterar Core;
- não reintroduzir V2;
- não introduzir filas;
- não mudar o contrato de publicação.

Somente uma falha bloqueadora reproduzida no CATCH-UP pode justificar alteração. Qualquer alteração exige nova suíte e nova certificação.

---

## 15. Checklist

| Módulo | Estado |
|---|---|
| Coordinator | ✅ fechado |
| SQLite / Database | ✅ fechado |
| Storage | ✅ fechado |
| Recovery | ✅ fechado |
| Startup Audit | ✅ fechado |
| ArmoredSync | ✅ fechado |
| Telegram lifecycle | ✅ fechado |
| Vision V1 / Shopee | ✅ fechado |
| Caption Gemini V1 | ✅ fechado |
| Caption Policy | ✅ fechado |
| ArmoredStudio | ✅ fechado |
| RVC | ✅ fechado |
| FFmpeg / Finalizer | ✅ fechado + Story 9:16 |
| ArmoredHub | ✅ fechado |
| Telegram publication | ✅ fechado |
| Confirmation | ✅ fechado |
| Cleanup | ✅ fechado |
| START_ALL | ✅ fechado |
| Automated suite | ✅ verde |
| Vision V2 | 🚫 fora da versão |
| CATCH-UP histórico ≈308 | ⏳ último teste operacional |
| LIVE novo conteúdo | ⏳ não reproduzível artificialmente |
| Certificação histórica 100% | ⏳ após CATCH-UP |

---

## 16. Regra operacional final

Até o CATCH-UP histórico completo, o estado correto é:

```text
CÓDIGO
└── FECHADO / CONGELÁVEL

E2E REAL
└── COMPROVADO

RECOVERY
└── COMPROVADO

CAPTION V1 + GEMINI
└── COMPROVADO

V2
└── REMOVIDA

HISTÓRICO ≈308
└── ÚLTIMA CERTIFICAÇÃO OPERACIONAL
```

Depois que o histórico terminar e o Coordinator entrar em LIVE, registrar o commit exato, contagens finais, checkpoints, órfãos, recoveries e estado final no README. A partir daí, a versão poderá ser marcada como certificação histórica completa.
