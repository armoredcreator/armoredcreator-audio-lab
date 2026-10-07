# ArmoredCreator Audio Lab
## Multisource + Vision-before-Download + Audio Intelligence

> **Estado da implementação:** laboratório de evolução do comportamento já comprovado do ArmoredCreator.
>
> **Repositório de referência:** `armoredcreator/armoredcreator-test`
>
> **Repositório do Lab:** `armoredcreator/armoredcreator-audio-lab`
>
> **Branch de desenvolvimento:** `fix/multisource-storage-audio-intelligence`
>
> **Objetivo desta versão:** adicionar a Fonte 2, isolar fisicamente os workspaces por fonte, manter SQLite compartilhado como fonte de verdade, introduzir o gate Vision-before-Download e tornar o Studio consciente do tipo de áudio, sem criar uma pipeline paralela nem reintroduzir filas físicas.

---

# 1. O que esta versão é

Esta versão do **ArmoredCreator Audio Lab** é uma evolução aditiva da arquitetura já validada na referência.

A referência continua sendo a autoridade de comportamento. O Lab adiciona somente os mecanismos necessários para:

1. operar mais de uma fonte Telegram;
2. associar cada fonte ao seu próprio destino Hub;
3. manter checkpoints independentes por fonte/tópico;
4. impedir colisão quando duas fontes usam o mesmo ID de mensagem;
5. separar fisicamente os workspaces de mídia;
6. executar a **Vision V1 antes do download**;
7. analisar o áudio do vídeo já aceito e selecionar o comportamento correto de RVC;
8. preservar Recovery, idempotência, confirmação Telegram e cleanup.

A regra central continua sendo:

**um único item ativo por vez.**

---

# 2. Arquitetura final

A composição continua centralizada no `Coordinator`.

```
                 +----------------------+
                 |      Coordinator      |
                 |  única raiz de vida   |
                 +----------+-----------+
                            |
                            v
                    +---------------+
                    | ArmoredSync   |
                    | Fonte 1 / 2   |
                    +-------+-------+
                            |
                     candidato único
                            |
                            v
                    +---------------+
                    | SQLite        |
                    | RESERVE       |
                    | sem download  |
                    +-------+-------+
                            |
                            v
                    +---------------+
                    | Vision V1     |
                    | URL -> produto|
                    +-------+-------+
                       /          \
          sem resolução            resolvido
                |                    |
                v                    v
       WAITING_VISION        persistir affiliate_url
                                     |
                                     v
                              materializar ORIGINAL
                                     |
                                     v
                                ArmoredIA
                                     |
                                     v
                              ArmoredStudio
                              audio profile + RVC
                                     |
                                     v
                                ArmoredHub
                                     |
                                     v
                              Telegram destino
                                     |
                              CONFIRMED / ABSENT
                                     |
                                     v
                                 PUBLISHED
                                     |
                                     v
                                  cleanup
                                     |
                                     v
                              próximo candidato
```

Não existem consumidores paralelos de fila. O próximo conteúdo só é exposto depois que o item ativo alcança uma condição durável segura.

---

# 3. Invariantes que não podem ser quebrados

## 3.1 Coordinator é a única composição

O `Coordinator.build()` monta as dependências e define a ordem operacional:

- Sync;
- Database;
- Vision;
- ArmoredIA;
- Studio;
- Hub;
- Recovery;
- Startup Audit.

Nenhum módulo inferior pode criar uma segunda pipeline operacional.

## 3.2 Um único item ativo

Não existe pré-download de lote.

O fluxo é:

```
descobrir A
-> reservar A
-> Vision V1
-> se aceito: materializar A
-> ArmoredIA
-> Studio
-> Hub
-> confirmação
-> cleanup
-> descobrir B
```

Mesmo com duas fontes, o `MultiTelegramSource` mantém somente um candidato pendente atravessando a fronteira Sync -> Coordinator.

## 3.3 Nenhuma fila física

Não fazem parte da arquitetura:

- RabbitMQ;
- Redis;
- Celery;
- Kafka;
- `publish_queue`;
- pastas usadas como fila operacional.

SQLite + checkpoints são a coordenação durável.

## 3.4 SQLite é a fonte de verdade

Banco canônico:

```
storage/database/armoredcreator.db
```

O banco mantém, entre outros:

```
items
state_events
publications
sync_state
sync_topics
sync_source_topics
sync_sources
caption_candidates
runtime_locks
```

Também registra:

- estado do item;
- URL original;
- `affiliate_url`;
- `affiliate_name`;
- contexto Vision;
- contexto ArmoredIA;
- candidatos de caption;
- paths dos artefatos;
- SHA256 do ORIGINAL;
- tentativas;
- Recovery;
- cleanup;
- publication;
- message_id publicado;
- status de confirmação;
- checkpoints por fonte.

## 3.5 Original é imutável

O ORIGINAL é o ponto de recuperação.

Working e Result são derivados.

Recovery nunca deve depender de um arquivo derivado parcial quando existe ORIGINAL válido.

---

# 4. Multisource

## 4.1 Rotas atuais

A configuração de produção da evolução é:

| Fonte | Telegram | source_id | Hub | Tópico |
|---|---|---|---|---:|
| Fonte 1 | `-1003788989075` | `-1003788989075` | `-1004341972306` | 228 |
| Fonte 2 | `-1002698134896` | `-1002698134896` | `-1004341972306` | 1160 |

Cada rota é definida por:

```
SourceConfig
    key
    chat_id
    source_id

HubConfig
    key
    chat_id
    topic_id

RouteConfig
    source
    hub
```

Arquivo responsável:

```
armored_core/routing.py
```

## 4.2 Isolamento de identidade

O mesmo Telegram message ID pode existir legitimamente em duas fontes diferentes.

Exemplo:

```
Fonte 1 -> message_id 77
Fonte 2 -> message_id 77
```

Não podem virar o mesmo item lógico.

A identidade interna de fontes reais é, portanto, escopada por fonte:

```
content_id = <source_id>_<telegram_message_id>
```

Exemplo:

```
-1002698134896_77
```

Registros históricos legados da primeira versão podem continuar com a identidade antiga. A migração preserva essa identidade e não destrói o estado certificado.

## 4.3 URL também é escopada por fonte

A mesma URL Shopee pode aparecer nas duas fontes e ser elegível independentemente.

A deduplicação do Sync usa:

```
source_id + original_url
```

Assim:

```
Fonte 1 + URL X
Fonte 2 + URL X
```

são conteúdos independentes.

---

# 5. Storage multisource

A separação física foi desenhada para manter a mesma estrutura interna do workspace, apenas mudando a raiz da fonte.

## 5.1 Estrutura

```
storage/
├── database/
│   └── armoredcreator.db
├── logs/
├── backups/
├── videos/
│   └── ... legado/compatibilidade
├── Videos GRUPO_FONTE_1/
│   └── <telegram_message_id>/
│       ├── <telegram_message_id>_<produto>.mp4
│       ├── <telegram_message_id>_.mp4
│       └── <telegram_message_id>_<resultado>.mp4
└── Videos GRUPO_FONTE_2/
    └── <telegram_message_id>/
        ├── <telegram_message_id>_<produto>.mp4
        ├── <telegram_message_id>_.mp4
        └── <telegram_message_id>_<resultado>.mp4
```

**Não existe:**

```
storage/sources/
```

A Fonte 2 é isolada diretamente por uma pasta de primeiro nível, conforme o padrão definido para o Lab.

**Importante:** o `content_id` do SQLite continua podendo ser escopado por `source_id` (por exemplo, `-1002698134896_77`) para impedir colisões entre fontes. Isso é separado do identificador físico do workspace: em cada pasta de fonte, o filesystem segue o padrão da Fonte 1 usando o próprio `telegram_message_id` (`77/`, `77_*.mp4`).

## 5.2 API de Storage

Arquivo:

```
armored_core/storage.py
```

A API mantém o mesmo modelo do workspace original:

```
workspace(...)
workspace_path(...)
original(...)
working(...)
result(...)
```

Agora todas as operações podem receber `source_id`.

Isso garante que:

- ORIGINAL;
- working;
- result;
- Recovery;
- Startup Audit

resolvam o mesmo workspace físico.

## 5.3 Regra de segurança

O caminho persistido em SQLite tem prioridade no Startup Audit.

Quando o banco possui `original_path`, o audit deriva o workspace de:

```
Path(original_path).parent
```

Isso impede que um item da Fonte 2 seja interpretado como se estivesse em `storage/videos/<id>`.

---

# 6. ArmoredSync

Arquivo principal:

```
ArmoredSync/service.py
```

Responsabilidades:

- conexão Telegram;
- descoberta histórica;
- descoberta LIVE;
- agrupamento de conteúdo;
- deduplicação por fonte;
- checkpoint;
- materialização;
- lifecycle da sessão Telethon;
- adaptação multisource.

---

# 7. Regra de descoberta do conteúdo Telegram

A nova arquitetura **não mudou a lógica histórica de associação de conteúdo**.

O Sync continua procurando um conteúdo elegível pelos padrões comprovados:

### Caso A — mesma mensagem

```
VIDEO + Shopee URL
```

### Caso B — mensagem seguinte

```
VIDEO
LINK Shopee na mensagem não-vídeo imediatamente seguinte
```

### Caso C — álbum Telegram

Um mesmo `grouped_id` pode conter:

```
video + link
video + image + link
image + image + video + link
link + image + video
image + video + link
```

e outras ordens equivalentes, desde que as mensagens pertençam ao mesmo grupo.

Quando existe **um único link Shopee distinto dentro do álbum**, o grupo pode representar um único conteúdo e a seleção do vídeo é determinística.

Quando existem links diferentes no mesmo grupo, o Sync não inventa associação. Somente associa automaticamente vídeos que tenham vínculo explícito suficiente.

## 7.1 O que o Sync não faz

O Sync não procura um vídeo com áudio específico para decidir o candidato.

A seleção do candidato é baseada em Telegram/conteúdo/URL.

A classificação de áudio acontece **depois**, no Studio, sobre o vídeo que o Sync já entregou.

---

# 8. Caso histórico 450/451/452

Esse caso continua documentado porque foi a evidência que fechou a regra de associação por `grouped_id`.

```
tópico      = 287
grouped_id  = 14295227350202649

450 = PHOTO + Shopee 5ardb8fozx
451 = PHOTO sem link
452 = VIDEO sem link
```

O conteúdo correto foi:

```
vídeo selecionado = 452
URL               = https://s.shopee.com.br/5ardb8fozx
```

A evidência histórica registrada anteriormente foi:

```
SQLite             = SIM
state              = PUBLISHED
cleanup_completed  = 1
published_message  = 1299
verification       = CONFIRMED
Hub                = correto
```

Este caso continua sendo a referência funcional para associação de álbum.

---

# 9. CATCH-UP por fonte

Cada fonte possui estado histórico próprio.

O banco possui:

```
sync_sources
    source_id
    historical_complete

sync_source_topics
    source_id
    topic_id
    topic_name
    last_seen_message_id
```

Portanto:

```
Fonte 1 -> CATCH-UP / LIVE
Fonte 2 -> CATCH-UP / LIVE
```

podem ter checkpoints diferentes.

## 9.1 Condição de conclusão

Uma fonte só entra em LIVE quando:

```
histórico esgotado
+ nenhum bloqueio técnico
+ candidatos processados/classificados
+ checkpoints seguros
```

No multisource, a entrada global em LIVE ocorre somente quando todas as fontes configuradas estiverem historicamente concluídas.

## 9.2 WAITING_VISION não é RECOVERY

Quando a Vision V1 não consegue resolver o produto:

```
RECEIVED
-> VISION
-> WAITING_VISION
```

Isso é uma classificação funcional persistente.

Quando ocorre uma falha técnica:

```
RECEIVED/VISION/IA/STUDIO/PUBLISHING
-> RECOVERY
```

RECOVERY representa trabalho incompleto que precisa de retomada segura.

## 9.3 Regra de checkpoint

O checkpoint nunca pode avançar apenas porque o Telegram foi lido.

Para um candidato que entrou no processamento normal, a conclusão segura exige:

```
pipeline completo
+ publicação confirmada
+ cleanup
```

Materialização sozinha não é conclusão.

---

# 10. LIVE multisource

A classe:

```
MultiTelegramSource
```

é uma camada sequencial sobre várias instâncias de `TelegramSource`.

Ela mantém:

```
_cursor
_last_source
_pending_source
_pending_message
_pending_checkpoints
```

## 10.1 Um único candidato pendente

Ao descobrir um candidato da Fonte 1:

```
Fonte 1 -> pending
```

a Fonte 2 não entrega outro candidato ao Coordinator enquanto o item atual não for concluído/commitado.

Depois:

```
commit Fonte 1
-> limpa pending
-> próxima rodada
-> Fonte 2
```

A implementação usa round-robin entre as fontes, sem criar concorrência de processamento.

---

# 11. Vision-before-Download

Esta é a principal evolução de fluxo desta branch.

## 11.1 Antes

O fluxo antigo precisava do ORIGINAL local para começar a pipeline.

## 11.2 Agora

O Sync pode entregar o candidato como metadado + materializer, sem baixar a mídia.

O Coordinator faz:

```
1. descobrir candidato
2. reservar no SQLite
3. executar Vision V1 com a URL/evidência
4. persistir resultado
5. somente se aceito, materializar ORIGINAL
6. continuar pipeline normal
```

Isso evita baixar um vídeo quando a Vision não conseguiu resolver um produto válido.

## 11.3 Fluxo aceito

```
Sync
-> reserve
-> Vision V1
-> affiliate_url persistida
-> materialização
-> ArmoredIA
-> Studio
-> Hub
-> CONFIRMED
-> PUBLISHED
-> cleanup
```

## 11.4 Fluxo sem produto

```
Sync
-> reserve
-> Vision V1
-> WAITING_VISION
-> zero download
-> zero publicação
```

## 11.5 Falha técnica na Vision

```
Sync
-> reserve
-> Vision
-> falha técnica
-> RECOVERY
-> zero download
```

O candidato permanece recuperável pela fonte.

---

# 12. ArmoredVision V1

Arquivos principais:

```
ArmoredVision/service.py
ArmoredVision/modules/v1/shopee_api.py
ArmoredVision/modules/v1/shopee_resolver.py
```

A V1 continua responsável pela identificação do produto Shopee.

Ela não foi substituída por um novo módulo V2 nesta branch.

## 12.1 Fluxo

```
URL
-> resolver
-> produto exato
-> affiliate_url canônica
-> contexto de produto
-> SQLite
```

## 12.2 Evidência persistida

A V1 pode persistir:

```
affiliate_name
affiliate_url
affiliate_urls_json
ia_context_json
publication_caption
```

A URL canônica persistida é a evidência que permite ao Coordinator liberar a materialização.

---

# 13. ArmoredIA

Estrutura:

```
ArmoredIA/
├── service.py
├── providers/
│   ├── base.py
│   └── gemini.py
└── caption/
    ├── generator.py
    ├── policy.py
    └── selector.py
```

ArmoredIA recebe o contexto já resolvido pela Vision.

Não deve redescobrir o produto Shopee.

---

# 14. Caption

## 14.1 Batch

O provider pode retornar até:

```
ARMORED_IA_MAX_CANDIDATES=10
```

O pipeline avalia todas as candidatas localmente.

```
1 chamada Gemini
-> candidatas
-> Policy local
-> score local
-> seleção
```

A primeira candidata recebida não é automaticamente a vencedora.

## 14.2 Policy

Contrato:

```
2 ou 3 palavras
+ exatamente 1 emoji
+ 1 ou 2 hashtags
```

São bloqueados, entre outros:

- termos de venda;
- promoção;
- urgência;
- embalagem;
- ficha técnica;
- unidades e medidas;
- marca/modelo;
- combinações distintivas do nome do produto.

A Policy é local e determinística.

## 14.3 Evidência

Cada candidata pode ficar auditável no SQLite:

```
content_id
batch_id
candidate_index
caption
policy_valid
rejection_reason
score
selected
created_at
```

---

# 15. ArmoredStudio

Arquivos principais:

```
ArmoredStudio/service.py
ArmoredStudio/unified.py
ArmoredStudio/analysis/*
ArmoredStudio/processing/*
```

O Studio recebe o vídeo já materializado.

A partir daí, a decisão de áudio é feita **sobre o próprio vídeo processado**.

Não existe procura de “um vídeo ideal” por áudio.

---

# 16. Audio Intelligence

Arquivo novo:

```
ArmoredStudio/analysis/audio_profile.py
```

Tipos atuais:

```
NO_AUDIO
MUSIC_ONLY
SPEECH
SPEECH_PLUS_MUSIC
```

O analisador usa:

- existência de stream de áudio;
- RMS;
- peak;
- WebRTC VAD.

A detecção é baseada em presença de voz, não em idioma.

Assim:

```
Português -> fala
Inglês     -> fala
Espanhol   -> fala
outro idioma com voz -> fala
```

A origem do conteúdo não precisa ser conhecida para decidir RVC.

## 16.1 Regras de processamento

### SPEECH

```
áudio original
-> RVC Melody
-> narração permanece clara
-> música principal mais baixa
```

### SPEECH_PLUS_MUSIC

```
áudio original
-> RVC Melody
-> música de fundo abaixo da narração
-> intro/final com tratamento alto
```

### MUSIC_ONLY

```
não executa RVC
-> substitui áudio por silêncio
-> música/efeito do Studio continua sendo usado
-> música principal recebe tratamento alto
```

### NO_AUDIO

```
não executa RVC
-> cria silêncio intermediário
-> música/efeito do Studio continua sendo usado
-> música principal recebe tratamento alto
```

A classificação de `MUSIC_ONLY` e `NO_AUDIO` **não faz o Sync procurar outro candidato**. O próprio vídeo entregue pelo Sync é analisado.

## 16.2 Ganho

O finalizer recebe o perfil de áudio e utiliza ganho diferente para conteúdo com e sem fala.

Limites importantes:

```
MUSIC_VOLUME          = 0.8
INTRO_MUSIC_VOLUME    = 2.5   (teto)
```

O cálculo de intro/final é adaptativo ao nível do áudio, limitado para evitar valores patológicos.

## 16.3 RVC

Quando existe fala:

```
audio_original
-> converter_voz(...)
-> audio_rvc
```

Quando não existe fala:

```
audio_original/silêncio
-> copia para audio_rvc
```

Não há conversão RVC fictícia de uma faixa sem fala.

---

# 17. Finalização de vídeo

O finalizer continua responsável pelo encadeamento FFmpeg, história visual, música e efeito.

A regra nova só altera a fonte de áudio e o ganho da música conforme `AudioProfile`.

O restante do contrato visual da referência permanece.

Arquivos derivados temporários de áudio são removidos após a finalização.

---

# 18. ArmoredHub

Arquivo:

```
ArmoredHub/service.py
```

O Hub continua sendo uma fronteira de efeito externo.

## 18.1 Destino por rota

Para um item novo:

```
item.source_id
-> RouteConfig
-> hub.chat_id + hub.topic_id
```

Fonte 1:

```
-1004341972306 / tópico 228
```

Fonte 2:

```
-1004341972306 / tópico 1160
```

Se já existir destino persistido na publication, ele tem prioridade durante a reconciliação.

## 18.2 Idempotência

Antes de criar um novo efeito externo, o Hub consulta a publication e a evidência Telegram existente.

A regra continua:

```
publication existente
-> reconciliar
-> só publicar novamente quando ABSENT seguro
```

## 18.3 CONFIRMED

Só existe confirmação quando há mensagem real e `message_id` válido.

## 18.4 ABSENT

ABSENT exige evidência suficiente de que a publicação não ocorreu.

## 18.5 UNKNOWN

UNKNOWN significa que a evidência não é suficiente.

```
UNKNOWN
-> RECOVERY
-> não republicar automaticamente
```

UNKNOWN não pode virar sucesso por conveniência.

---

# 19. Recovery

Arquivo:

```
armored_core/recovery.py
```

Recovery é baseado em evidência durável.

## 19.1 Falha sem resultado

Se não existe resultado final confiável:

```
ORIGINAL
-> reconstruir derivados
-> Studio
-> Hub
```

## 19.2 Resultado durável

Se existe resultado comprovadamente válido:

```
RESULT
-> PUBLISHING
-> confirmação
-> cleanup
```

sem rerodar Studio desnecessariamente.

## 19.3 Studio quebrado

Resultado derivado possivelmente parcial não é tratado como confiável somente porque existe.

O Recovery reconstrói a cadeia a partir do ORIGINAL quando a evidência indica falha de processamento.

## 19.4 Falta do ORIGINAL

Se o item está em `RECEIVED` e o ORIGINAL não existe:

- Recovery não inventa bytes;
- remove `.part` quando possível;
- mantém a reserva no SQLite;
- deixa o Sync redescobrir/materializar novamente.

Esse comportamento é especialmente importante com o gate Vision-before-Download.

---

# 20. Startup Audit

Arquivo:

```
armored_core/startup_audit.py
```

A auditoria ocorre antes de abrir o fluxo normal do Sync.

Ela verifica:

- itens SQLite;
- estados;
- workspaces;
- ORIGINAL;
- working;
- result;
- cleanup;
- publications;
- confirmação Telegram;
- órfãos.

## 20.1 Multisource

A varredura usa:

```
Storage.video_roots()
```

para examinar:

```
storage/videos/
storage/Videos GRUPO_FONTE_1/
storage/Videos GRUPO_FONTE_2/
...
```

sem criar diretórios durante a simples descoberta dos roots.

## 20.2 Migração legada

O audit pode corrigir casos históricos inequívocos em que uma falha de Caption antiga foi registrada como `WAITING_VISION`.

Somente evidência explícita de falha de Caption permite:

```
WAITING_VISION
-> RECOVERY
```

Isso preserva o significado atual de WAITING_VISION como estado da Vision.

---

# 21. Sessão Telegram e reconexão

Arquivo principal:

```
ArmoredSync/service.py
```

A sessão Telethon fica em:

```
credentials/telegram/session/armoredsync
```

A rotina de reconexão fecha o cliente anterior antes de criar outro.

Objetivo:

```
disconnect
-> close session
-> rebuild client
-> connect
```

Isso evita disputas de SQLiteSession e o problema histórico de `database is locked` no Windows.

---

# 22. Falhas transitórias de origem

Falha de rede/Telegram não deve matar o Coordinator.

Em CATCH-UP:

```
erro transitório
-> liberar conexão
-> reconstruir iterator
-> retry/backoff
-> manter checkpoint seguro
```

Em LIVE:

```
erro transitório
-> não avançar checkpoint
-> manter processo vivo
-> próxima iteração reconecta
```

Uma falha de download não transforma automaticamente o item em sucesso nem permite que o checkpoint “pule” o candidato unresolved.

---

# 23. Estados duráveis

```
RECEIVED
    |
    +--> VISION
    |      |
    |      +--> WAITING_VISION
    |      |
    |      +--> IA
    |             |
    |             +--> STUDIO
    |                    |
    |                    v
    |                PUBLISHING
    |                    |
    |                    v
    |                PUBLISHED
    |
    +--> RECOVERY
           |
           +--> retry/rebuild
```

Também existe `FAILED` para compatibilidade com estados legados. O caminho moderno trata falhas recuperáveis como `RECOVERY`.

---

# 24. Sequência operacional completa

## 24.1 CATCH-UP

```
Startup Audit
-> Recovery pendente

Fonte 1 / Fonte 2
-> descobrir um candidato
-> reservar no SQLite
-> Vision V1
    -> não resolveu -> WAITING_VISION
    -> resolveu -> affiliate_url persistida
-> materializar ORIGINAL
-> ArmoredIA
-> Studio
-> Hub
-> Telegram CONFIRMED
-> PUBLISHED
-> cleanup
-> checkpoint
-> próximo candidato
```

## 24.2 LIVE

```
buscar candidato em uma das fontes
-> manter pending
-> Vision
-> materialização
-> pipeline
-> confirmação
-> cleanup
-> commit do checkpoint
-> liberar pending
-> próxima fonte/candidato
```

## 24.3 Restart

```
processo reinicia
-> Startup Audit
-> Recovery
-> verificar publication
-> recuperar resultado/derivados
-> destravar apenas quando houver evidência segura
-> continuar CATCH-UP/LIVE
```

---

# 25. Credenciais e configuração

## 25.1 Fonte de segredos

O arquivo de segredos é:

```
credentials/project.env
```

O Coordinator carrega esse arquivo primeiro com:

```
load_dotenv(project_credentials, override=True)
```

Configuração pública permanece no `.env`.

Segredos não devem entrar no Git.

## 25.2 Configuração multisource

Exemplo atual:

```
ARMORED_SOURCE_1_CHAT_ID=-1003788989075
ARMORED_SOURCE_1_ID=-1003788989075
ARMORED_SOURCE_1_KEY=source1
ARMORED_SOURCE_1_VIDEO_DIR=Videos GRUPO_FONTE_1

ARMORED_SOURCE_2_CHAT_ID=-1002698134896
ARMORED_SOURCE_2_ID=-1002698134896
ARMORED_SOURCE_2_KEY=source2
ARMORED_SOURCE_2_VIDEO_DIR=Videos GRUPO_FONTE_2

ARMORED_HUB_1_CHAT_ID=-1004341972306
ARMORED_HUB_1_TOPIC_ID=228

ARMORED_HUB_2_CHAT_ID=-1004341972306
ARMORED_HUB_2_TOPIC_ID=1160
```

## 25.3 Compatibilidade legada

Ainda existe fallback para configuração de uma única fonte:

```
ARMORED_SYNC_SOURCE
ARMORED_SYNC_SOURCE_ID
ARMORED_CREATOR_GROUP_ID
ARMORED_HUB_TOPIC_ID
```

Esse fallback não substitui a configuração multisource quando `ARMORED_SOURCE_N_*` está preenchida.

## 25.4 ArmoredIA

```
ARMORED_IA_ENABLED=1
ARMORED_IA_CAPTION_ENABLED=1
ARMORED_IA_MODEL=gemini-3.1-flash-lite
ARMORED_IA_API_TIMEOUT=90
ARMORED_IA_MAX_CANDIDATES=10
ARMORED_IA_MAX_ATTEMPTS=5
ARMORED_IA_RETRY_DELAY=2
ARMORED_IA_AUDIENCE=público brasileiro de descoberta e lifestyle
```

## 25.5 Telegram

```
ARMORED_TELEGRAM_CONNECTION_POOL_SIZE=4
ARMORED_TELEGRAM_CONNECT_TIMEOUT=15
ARMORED_TELEGRAM_READ_TIMEOUT=60
ARMORED_TELEGRAM_WRITE_TIMEOUT=180
ARMORED_TELEGRAM_POOL_TIMEOUT=15
```

---

# 26. START_ALL

O launcher operacional continua sendo:

```
START_ALL.bat
```

Ele é responsável por preparar o ambiente e iniciar o Coordinator.

O launcher não cria uma pipeline paralela.

Quando configurado, o laboratório pode usar Bot API local para envio enquanto Telethon permanece responsável pela reconciliação/leitura necessária.

---

# 27. Observabilidade

## 27.1 Console

O terminal mostra:

- item;
- etapa;
- resultado essencial;
- erros reais;
- progresso operacional.

Dump detalhado de modelos e parâmetros internos não deve poluir o console.

## 27.2 Trace

Arquivo:

```
storage/logs/pipeline_trace.jsonl
```

O trace mantém eventos estruturados de:

- START;
- END;
- TRANSITION;
- resultado Vision;
- resultado IA;
- Recovery;
- publicação;
- confirmação;
- erros.

## 27.3 Logs de Studio/RVC

Os detalhes técnicos permanecem nos logs apropriados.

---

# 28. Testes automatizados

A suíte cobre a arquitetura herdada e os contratos adicionados nesta evolução.

Entre os cenários protegidos:

### Arquitetura

- ausência de filas físicas;
- ausência de `storage/sources`;
- launcher único;
- ausência de caminhos Windows hard-coded.

### Database

- migração de identidade Telegram;
- identidade por fonte;
- checkpoints por fonte;
- preservação do estado histórico;
- publication e Recovery.

### Storage

- workspace Fonte 1;
- workspace Fonte 2;
- isolamento físico;
- mesmo message ID em fontes diferentes;
- resultado durável no root correto.

### Sync

- descoberta histórica;
- agrupamento por `grouped_id`;
- deduplicação de URL por fonte;
- checkpoint histórico;
- LIVE;
- reconnect;
- um único candidato pendente;
- commit na fonte correta.

### Vision gate

```
Vision aceita
-> materializer chamado

Vision não resolve
-> WAITING_VISION
-> materializer NÃO chamado

Vision falha tecnicamente
-> RECOVERY
-> materializer NÃO chamado

Vision aceita + download falha
-> checkpoint bloqueado
-> candidato continua recuperável
```

### Audio

- NO_AUDIO;
- MUSIC_ONLY;
- SPEECH;
- SPEECH_PLUS_MUSIC;
- presença de fala;
- ganho adaptativo;
- RVC somente quando existe fala.

### Recovery

- recuperação de Studio;
- recuperação de publicação;
- resultado durável;
- restart;
- source-aware workspace;
- cleanup.

---

# 29. Evidência de testes do estado atual

A validação integral mais recente concluída antes desta atualização documental foi:

```
GitHub Actions
run #100
job: unit

204 passed
1 skipped
28.15s
```

Commit validado:

```
368418d19cf3763b4e026001ef82970a76c4bd2b
```

A versão atual do README está sendo atualizada como documentação da mesma branch.

**Importante:** esse número comprova a suíte automatizada. Ele não substitui um E2E real novo com as duas fontes Telegram nesta mesma rodada.

---

# 30. Evidências reais herdadas da referência

Os seguintes resultados já faziam parte da certificação anterior e permanecem como evidência de comportamento herdado:

- CATCH-UP histórico da Fonte 1;
- caso 450/451/452;
- Recovery real;
- reconciliação Telegram;
- publicações confirmadas;
- cleanup;
- conteúdo LIVE observado.

Essa documentação preserva essas evidências, mas não as apresenta como se tivessem sido reexecutadas do zero no commit atual.

---

# 31. O que mudou em relação à referência

## Adicionado

```
armored_core/routing.py
ArmoredStudio/analysis/audio_profile.py
tests/test_audio_profile.py
tests/test_feature_contracts_gate_storage_audio.py
tests/test_multisource_routing.py
tests/test_storage_multisource.py
tests/test_vision_before_download.py
scripts/e2e_multisource_contracts_real.py
pytest.ini
```

## Estendido

```
armored_core/coordinator.py
armored_core/database.py
armored_core/storage.py
armored_core/services.py
armored_core/pipeline.py
armored_core/recovery.py
armored_core/startup_audit.py
ArmoredSync/service.py
ArmoredHub/service.py
ArmoredStudio/unified.py
ArmoredStudio/processing/finalizer.py
```

## Não removido

Nenhum arquivo da árvore da referência foi removido pela evolução.

A comparação estrutural da árvore encontrou:

```
110 arquivos comuns
92 idênticos byte a byte
18 alterados
9 novos
0 removidos
```

Isso é importante porque a estratégia desta branch é extensão controlada, não reescrita.

---

# 32. O que esta versão não faz

Esta versão não:

- cria uma nova Vision V2 dentro da V1;
- procura candidatos por tipo de áudio;
- usa áudio para decidir qual vídeo baixar;
- cria filas físicas;
- pré-baixa lotes;
- usa `storage/sources/`;
- duplica SQLite por fonte;
- permite dois itens ativos simultaneamente;
- trata UNKNOWN como confirmação;
- faz republicação automática em UNKNOWN;
- considera materialização como conclusão;
- declara a nova Fonte 2 como historicamente certificada por evidência que não foi executada.

---

# 33. Critérios de fechamento da arquitetura

Para considerar a implementação fechada nesta branch, todos estes pontos precisam permanecer verdadeiros:

```
Coordinator = única composição
SQLite = source of truth
1 item ativo = SIM
filas físicas = NÃO
pré-download de lote = NÃO

Source 1 isolada = SIM
Source 2 isolada = SIM
checkpoints independentes = SIM
same message ID entre fontes = seguro
same URL entre fontes = independente

Vision antes do download = SIM
WAITING_VISION sem download = SIM
Vision failure sem download = SIM

SPEECH -> RVC = SIM
SPEECH_PLUS_MUSIC -> RVC = SIM
MUSIC_ONLY -> sem RVC + silêncio = SIM
NO_AUDIO -> sem RVC + silêncio = SIM

Recovery preserva ORIGINAL = SIM
UNKNOWN não republica = SIM
cleanup fecha o item = SIM
```

---

# 34. Regra operacional definitiva

A ordem de vida de um item é:

```
DISCOVER
   |
RESERVE
   |
VISION GATE
   |
   +--> WAITING_VISION
   |
   +--> ACCEPT
          |
      MATERIALIZE
          |
        IA
          |
       STUDIO
          |
         HUB
          |
      CONFIRMED
          |
      PUBLISHED
          |
       CLEANUP
          |
     CHECKPOINT
          |
      NEXT ITEM
```

Recovery é uma via transversal baseada em evidência:

```
qualquer estágio incompleto
        |
        v
     RECOVERY
        |
        +--> retomar resultado durável
        +--> reconstruir derivados
        +--> repetir IA quando necessário
        +--> voltar à Vision quando necessário
```

O princípio permanece simples:

**um item, uma fonte, um workspace, um ciclo durável por vez.**

---

# 35. Manutenção

Ao alterar esta branch:

1. preservar o comportamento da referência quando não houver motivo explícito para mudá-lo;
2. adicionar testes junto com qualquer nova regra;
3. manter Source 1 funcional;
4. manter Source 2 apenas como extensão;
5. não introduzir filas;
6. não mover o SQLite para roots separados;
7. não criar `storage/sources/`;
8. não permitir que checkpoint avance além de um item não resolvido;
9. não baixar antes da Vision quando o fluxo estiver usando o gate;
10. não declarar E2E real como comprovado sem evidência real correspondente.

A referência continua sendo:

```
armoredcreator/armoredcreator-test
```

O desenvolvimento desta evolução continua em:

```
armoredcreator/armoredcreator-audio-lab
```

---

# 36. Estado deste Lab

**Branch:**

```
fix/multisource-storage-audio-intelligence
```

**Última validação integral de testes antes da atualização deste README:**

```
204 passed, 1 skipped
```

**Último commit de código validado:**

```
368418d19cf3763b4e026001ef82970a76c4bd2b
```

**PR:** #4

O PR permanece voltado à evolução multisource + Vision-before-Download + Audio Intelligence, com a Fonte 1 preservada como comportamento de referência.
