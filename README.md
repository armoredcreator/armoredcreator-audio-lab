# ArmoredCreator — Arquitetura Final, Contrato Operacional e Certificação

> Laboratório de reconstrução e certificação: armoredcreator/armoredcreator-test.
> O repositório oficial armoredcreator/armoredcreator e a branch audit/baseline-2026-09-19 permanecem intocados.

Este documento é a referência operacional da versão atual. Ele descreve o fluxo completo, os limites de cada componente, as regras de recuperação, a publicação Telegram, a política de legenda, a observabilidade e os critérios para a certificação histórica.

---

# 1. Visão geral

O ArmoredCreator recebe conteúdos da fonte Telegram, encontra o produto Shopee exato, gera o contexto para a IA, processa o vídeo, publica o resultado e só finaliza o item depois da confirmação externa e do cleanup.

O princípio central é um único item ativo por vez.

~~~
Telegram fonte
  |
  v
ArmoredSync
  |
  v
SQLite + workspace canônico
  |
  v
Coordinator
  |
  +----> ArmoredVision V1
  |          |
  |          +----> contexto V1 persistido
  |
  +----> ArmoredIA
  |          |
  |          +----> legenda válida
  |
  +----> ArmoredStudio
  |          |
  |          +----> vídeo final
  |
  +----> ArmoredHub
             |
             v
       Telegram destino
             |
             v
        reconciliação
             |
             v
          CONFIRMED
             |
             v
          PUBLISHED
             |
             v
           cleanup
~~~

---

# 2. Invariantes

## 2.1 Uma única raiz de composição

O Coordinator é a única raiz que monta Sync, Vision, ArmoredIA, Studio, Hub e Recovery.

## 2.2 Um único item ativo

~~~
descobrir A
-> materializar A
-> Vision
-> ArmoredIA
-> Studio
-> Hub
-> confirmação
-> cleanup
-> descobrir B
~~~

Não existe pré-download de lote.

## 2.3 Nenhuma fila física

Não fazem parte da arquitetura RabbitMQ, Redis, Celery, Kafka, publish_queue ou pastas intermediárias usadas como fila.

## 2.4 SQLite é a fonte de verdade interna

Banco canônico: storage/database/armoredcreator.db

Ele guarda estado, eventos, tentativas, Recovery, checkpoints, contexto V1, contexto ArmoredIA e publicações.

## 2.5 Workspace canônico

Cada item usa storage/videos/{content_id}/.

O ORIGINAL é o artefato imutável de recuperação. Working e result são derivados.

## 2.6 Cleanup

Cleanup só ocorre depois de PUBLISHED e só então o item pode ser considerado concluído fisicamente.

---

# 3. Máquina de estados

~~~
RECEIVED
VISION
IA
STUDIO
PUBLISHING
PUBLISHED
WAITING_VISION
RECOVERY
FAILED
~~~

Fluxo normal com IA habilitada:

~~~
RECEIVED -> VISION -> IA -> STUDIO -> PUBLISHING -> PUBLISHED
~~~

## 3.1 WAITING_VISION

WAITING_VISION significa exclusivamente que a Vision V1 não conseguiu resolver o produto Shopee exato e, portanto, o item não possui destino de publicação naquele momento.

É um estado persistente de conteúdo sem destino por enquanto:

~~~
Vision não encontrou produto Shopee
-> WAITING_VISION
-> permanece no SQLite
-> não publica
-> não é Recovery
-> não bloqueia o CATCH-UP
-> não impede a entrada em LIVE
~~~

Falha de Gemini, Policy, ArmoredIA, Studio ou Hub não deve ser classificada como WAITING_VISION.

## 3.2 RECOVERY

RECOVERY é o estado técnico persistente para um item que precisa ser retomado.

~~~
evidência suficiente -> retomar no ponto seguro
evidência insuficiente -> reconstruir derivados a partir do ORIGINAL
~~~

---

# 4. ArmoredSync

Arquivo principal: ArmoredSync/service.py

Responsabilidades:

- conectar à fonte Telegram;
- descobrir histórico;
- descobrir LIVE;
- localizar candidatos;
- materializar um único candidato;
- controlar checkpoints;
- administrar o lifecycle da sessão Telethon.

## 4.1 Regra de candidato

Um candidato válido pode ocorrer de três formas:

- vídeo e link Shopee na mesma mensagem;
- vídeo seguido imediatamente pela mensagem não-vídeo que contém o link Shopee;
- álbum Telegram (grouped_id) contendo vídeo(s), foto(s) e um único link Shopee distribuído entre as mídias, em qualquer ordem.

Dentro de um álbum com um único link Shopee, o grupo representa um único conteúdo e um vídeo é escolhido deterministicamente para materialização. Se houver links diferentes no mesmo álbum, somente vídeos que carregam explicitamente seu próprio link são associados; associações ambíguas não são adivinhadas.

O mesmo link Shopee já representado no SQLite não gera uma nova coleta. A deduplicação por URL ocorre na descoberta do Sync; o SyncService de ingestão continua mantendo IDs Telegram distintos como registros independentes quando chamado diretamente.

Não existe busca arbitrária por links em mensagens não relacionadas.

## 4.2 CATCH-UP

O CATCH-UP histórico é progressivo e sequencial.

~~~
descobrir candidato
-> materializar
-> processar
-> confirmar
-> cleanup
-> próximo candidato
~~~

O checkpoint nunca pode saltar um predecessor que ainda não tenha conclusão segura.

`WAITING_VISION` é uma conclusão segura de classificação para fins de CATCH-UP: o item continua persistido, mas seu checkpoint pode avançar quando não existe um bloqueio técnico anterior. `RECOVERY`, ao contrário, mantém o checkpoint bloqueado até resolução.

## 4.3 LIVE

LIVE usa checkpoints persistidos e preserva o contrato de um único item ativo.

Timeout de descoberta LIVE não é timeout do Studio ou do RVC.

---

# 5. Lifecycle Telegram do Sync

A sessão canônica do Sync fica em credentials/telegram/session/armoredsync.

Reconexão segura:

~~~
desconectar
-> fechar sessão
-> reconstruir client
-> conectar
~~~

Esse ciclo existe para evitar disputa pela sessão SQLite e o antigo database is locked no Windows.

## 5.1 Indisponibilidade temporária da rede — pendência de fechamento

O teste real de 30/09 revelou dois comportamentos distintos:

~~~
internet cai durante download
-> materialização interrompida por 60s sem progresso
-> checkpoint não avança
-> candidato não é concluído
~~~

Essa parte funcionou conforme o contrato.

Porém, depois da falha, enquanto o Telegram ainda estava indisponível, as tentativas de reconexão esgotaram e o Coordinator encerrou com código 1.

Ainda não está comprovado o comportamento final desejado:

~~~
indisponibilidade temporária Telegram
-> manter Coordinator vivo
-> reconectar progressivamente
-> reencontrar o mesmo candidato
-> processar sem perder checkpoint
~~~

O caso está registrado como uma pendência específica de resiliência do runtime.

---

# 6. ArmoredVision V1

Arquivos principais:

~~~
ArmoredVision/service.py
ArmoredVision/modules/v1/shopee_api.py
ArmoredVision/modules/v1/shopee_resolver.py
~~~

A Vision V1 é responsável somente pela identificação Shopee e pela produção do contexto que a etapa de IA precisa.

## 6.1 Fluxo V1

~~~
URL original
-> resolver
-> shop_id + item_id
-> produto exato
-> affiliate link canônico
-> ia_context
-> SQLite
~~~

Vision não gera mais a legenda.
Vision não executa Gemini.
Vision não escolhe candidatas de legenda.

## 6.2 Contexto durável

~~~
productName
itemId
shopId
shopName
productCatIds
priceMin
priceMax
sales
ratingStar
brand
brandName
model
modelName
description
attributes
technicalCharacteristics
imageUrl
~~~

---

# 7. ArmoredIA

Estrutura:

~~~
ArmoredIA/
├── service.py
├── providers/
│   ├── base.py
│   └── gemini.py
└── caption/
    ├── generator.py
    ├── policy.py
    └── selector.py
~~~

ArmoredIA é a fronteira aberta para futuras IAs e futuras tarefas de IA.

Ela recebe contexto durável da Vision e não precisa redescobrir o produto.

---

# 8. ArmoredIA Caption

## 8.1 Batch de candidatas

O provider Gemini recebe uma única solicitação e pode devolver até 10 candidatas.

Contrato atualmente executado:

~~~
1 chamada Gemini
-> até 10 candidatas
-> Policy local
-> primeira válida
~~~

**Estado de fechamento:** o Selector atual ainda escolhe a primeira candidata válida. O desenho final desta versão exige comparar todas as candidatas válidas por uma regra local, determinística e testável, escolhendo a melhor sem realizar segunda chamada ao Gemini.

O comportamento final pretendido é:

~~~
1 chamada Gemini
-> até 10 candidatas
-> Policy local
-> todas as válidas
-> ranking/score local determinístico
-> melhor candidata
~~~


Se todas forem rejeitadas:

~~~
1 chamada
-> 0 válidas
-> erro de geração
-> RECOVERY
~~~

Exaustão de Policy não cria segunda chamada Gemini.

## 8.2 Retry

Retry é destinado a problemas técnicos do provider, como timeout, conexão, 429 e erros 5xx.

## 8.3 Sem fallback determinístico

Quando não existe uma legenda válida, a IA falha de forma explícita e o item fica recuperável.

## 8.4 Auditoria persistente das candidatas — pendência de fechamento

O SQLite é a fonte de verdade interna, porém a implementação atual ainda não possui uma tabela específica para registrar individualmente todas as candidatas retornadas pelo batch e suas decisões da Policy.

O contrato de fechamento exige persistir, por candidata:

~~~
content_id
batch_id
candidate_index
caption
policy_valid
rejection_reason
score
selected
created_at
~~~

A finalidade é permitir reconstruir, depois da execução, exatamente o que Gemini devolveu, por que cada opção foi aceita ou rejeitada e qual opção foi finalmente selecionada.

---

# 9. Caption Policy

Arquivo: ArmoredIA/caption/policy.py

A Policy é determinística e roda localmente após a resposta do provider.

## 9.1 Formato

~~~
2 ou 3 palavras no texto principal
exatamente 1 emoji
1 ou 2 hashtags
até 20 caracteres por hashtag
~~~

## 9.2 Bloqueios comerciais

Bloqueia linguagem de venda, promoção e urgência, incluindo compre, comprar, garanta, garantir, imperdível, aproveite, oferta, promoção, desconto, corra e não perca.

## 9.3 Embalagem

Bloqueia embalagem, tampa, frasco e lacre.

## 9.4 Especificações

Bloqueia medidas e especificações como ml, cm, g, kg, V, volts, W, watts e outras formas equivalentes quando usadas para expor a ficha técnica.

## 9.5 Marca e modelo

Marca e modelo do contexto V1 não devem aparecer na legenda. Modelos curtos também são protegidos.

## 9.6 Nome do produto

A Policy bloqueia combinações contíguas distintivas do título, sem proibir toda palavra que apareça no nome.

Exemplo:

~~~
Produto: Batom Matte Vermelho

Permitido:
Olha isso ✨
#beleza

Bloqueado:
Batom matte ✨
#beleza
~~~

## 9.7 Hashtag

Uma hashtag que reconstrua explicitamente uma combinação distintiva do título é rejeitada.

## 9.8 Contexto natural

Descrição, categoria, ambiente, uso e características comuns podem compartilhar palavras com a legenda.

Isso reduz falsos negativos e impede que a Policy vire um filtro excessivamente restritivo.

---

# 10. ArmoredStudio

Arquivos principais:

~~~
ArmoredStudio/service.py
ArmoredStudio/unified.py
ArmoredStudio/analysis/*
ArmoredStudio/processing/*
~~~

Fluxo:

~~~
ORIGINAL
-> análise
-> plano
-> RVC
-> FFmpeg
-> RESULT
~~~

## 10.1 RVC

Runtime padrão: ArmoredStudio/runtime/rvc/

Voz utilizada no ambiente real desta fase: melody.

RVC não possui downgrade silencioso.

## 10.2 Story

A saída final é 1080 x 1920 e mantém proporção, usando crop quando necessário.

## 10.3 Console

O console não deve mostrar dump do modelo ou parâmetros internos do backend.

Detalhes técnicos devem continuar disponíveis nos logs.

---

# 11. ArmoredHub

Arquivo principal: ArmoredHub/service.py

Responsabilidades:

- publicação;
- idempotência;
- reconciliação;
- confirmação;
- proteção contra republicação.

## 11.1 Transporte

O ambiente real usa Telegram Bot API local em 127.0.0.1:8081.

A reconciliação permanece baseada em Telethon.

## 11.2 publish_once

Para item novo:

~~~
publication inexistente
-> publish
-> message_id
-> confirmação
~~~

Não se deve pré-criar uma publication que faça item novo entrar no caminho de reconciliação como se já tivesse sido publicado. A chamada publish_once() pode registrar internamente o início da publicação depois de constatar que não existe registro anterior; isso é diferente de criar a publication antes da decisão de novo envio.

## 11.3 Hub Preflight — pendência de fechamento

O fluxo final deve validar a configuração essencial do Hub antes de consumir o processamento caro do Studio/RVC.

A ordem desejada é:

~~~
Vision
-> ArmoredIA
-> Hub Preflight
-> Studio/RVC
-> publicação
~~~

O preflight não publica e não substitui a reconciliação. Ele deve apenas verificar antecipadamente a configuração necessária para o efeito externo, incluindo destino, tópico, credenciais e transporte Bot API.

Se a configuração já for impossível, o item deve entrar em Recovery sem gastar o trabalho caro do Studio/RVC.

**Estado atual:** esse preflight ainda não foi implementado nem certificado.

## 11.4 CONFIRMED

CONFIRMED exige mensagem real e message_id válido.

## 11.5 UNKNOWN

UNKNOWN significa evidência insuficiente.

~~~
UNKNOWN
-> RECOVERY
-> não republicar automaticamente
~~~

## 11.6 ABSENT

ABSENT exige evidência suficiente de ausência.

Uma operação externa potencialmente executada não pode ser tratada como inexistente apenas porque uma busca inicial não encontrou evidência.

## 11.7 Reconciliação

~~~
busca contextual
-> candidato
-> verificação por message_id
-> confirmação exata
~~~

Metadado de tópico ausente pode ser tolerado quando a consulta não fornece esse campo.
Metadado explicitamente incompatível deve ser rejeitado.
Múltiplos matches não devem ser escolhidos arbitrariamente.

---

# 12. Recovery

Arquivo: armored_core/recovery.py

## 12.1 Falha da ArmoredIA

Quando V1 já resolveu e persistiu contexto:

~~~
VISION
-> IA
-> falha
-> RECOVERY
-> IA
-> STUDIO
~~~

A Vision não deve ser repetida somente por causa da falha da IA.

## 12.2 Falha do Studio

Derivados podem ser reconstruídos a partir do ORIGINAL.

## 12.3 Publicação pendente

Publication existente deve ser reconciliada antes de produzir um novo efeito externo.

## 12.4 Resultado durável

Resultado final comprovado pode ser retomado diretamente para publicação.

---

# 13. Startup Audit

Arquivo: armored_core/startup_audit.py

O startup verifica itens, estados, publicações, cleanup, workspaces, órfãos e evidências históricas.

Falhas legadas de Caption que foram classificadas incorretamente como WAITING_VISION podem ser migradas para RECOVERY quando a evidência for inequívoca.

Essa migração preserva o significado de WAITING_VISION como estado exclusivo da Vision V1.

---

# 14. Credenciais e configuração

Segredos ficam em credentials/project.env.
Configuração operacional fica em .env.

Valores secretos não são registrados no Git.

Configuração ArmoredIA usada no launcher:

~~~
ARMORED_IA_ENABLED=1
ARMORED_IA_CAPTION_ENABLED=1
ARMORED_IA_MODEL=gemini-3.1-flash-lite
ARMORED_IA_API_TIMEOUT=90
ARMORED_IA_MAX_CANDIDATES=10
ARMORED_IA_MAX_ATTEMPTS=5
ARMORED_IA_RETRY_DELAY=2
~~~

Destino do laboratório:

~~~
ARMORED_CREATOR_GROUP_ID=-1004341972306
ARMORED_HUB_TOPIC_ID=228
~~~

Fonte histórica do laboratório:

~~~
ARMORED_SYNC_SOURCE=-1003788989075
ARMORED_SYNC_SOURCE_ID=-1003788989075
~~~

O código atual descobre os tópicos do fórum e não usa ARMORED_SYNC_TOPIC_NAME como filtro exclusivo.

---

# 15. START_ALL

START_ALL.bat é o launcher operacional único.

Ele configura ArmoredIA, localiza e inicia o Bot API local, localiza Python e chama run_coordinator.py.

Ele não implementa uma pipeline paralela.

---

# 16. Observabilidade

## 16.1 Console

O console é a superfície do operador.

Deve mostrar somente item, etapa, progresso essencial, conclusão e erros reais.

## 16.2 Trace

Arquivo: storage/logs/pipeline_trace.jsonl

O trace mantém eventos estruturados de início, fim, duração, transições, Recovery, erros e confirmação.

## 16.3 Logs internos

RVC e análises podem manter detalhes completos em arquivo sem despejar esses dados no terminal.

---

# 17. Testes automatizados

A suíte cobre arquitetura, Coordinator, Database, Storage, Sync, Recovery, Startup Audit, Telegram lifecycle, CATCH-UP, LIVE, reconnect, Vision V1, ArmoredIA, Policy, provider Gemini, Studio, RVC, FFmpeg, Hub, reconciliação, idempotência e cleanup.

## 17.1 Estado atual do CI

No commit 73a8fd9d6e3b63272d8ee5e603e0d23720159a2a, os workflows 911 e 912 foram concluídos com sucesso. Ambos executaram a suíte completa com:

~~~
python -m pytest -q -W error::RuntimeWarning

160 passed
1 skipped
~~~

As alterações de observabilidade, portanto, possuem confirmação verde no CI. O teste local no ambiente Windows ainda deve ser repetido antes do freeze definitivo.

Comando principal:

~~~
python -m pytest -q -W error::RuntimeWarning
~~~

Verificação adicional:

~~~
git diff --check
~~~

O CI do commit atual já confirmou 160 passed, 1 skipped. O resultado passa a fazer parte da evidência desta versão.

---

# 18. Evidência operacional real em 29–30/09/2026

## Item 412

~~~
RECOVERY
-> VISION
-> IA
-> STUDIO/RVC
-> HUB
-> Telegram CONFIRMED #1005
-> PUBLISHED
-> cleanup
~~~

## Item 392

~~~
RECOVERY
-> VISION
-> IA
-> STUDIO/RVC
-> HUB
-> Telegram CONFIRMED #1007
-> PUBLISHED
-> cleanup
~~~

Esses casos comprovam integração real de Recovery, Vision, ArmoredIA, Studio, Hub, Telegram e cleanup.

Na certificação histórica zerada, também foram observados, com publicação real e cleanup:

~~~
1383
1174
823
706
564
563
698
550
767
~~~

Em todos esses casos o log observou a sequência necessária até PUBLISHED + cleanup.

O item 823 é uma evidência importante de download lento: 25,5 MiB foram materializados em aproximadamente 239,2s e o pipeline continuou normalmente até publicação e cleanup.

O item 564 produziu avisos do decoder H.264 (mmco: unref short failure) durante o Studio, mas terminou normalmente em Hub, confirmação Telegram e cleanup. O aviso não foi promovido a falha do pipeline.

Esses resultados comprovam operação real, mas não substituem a prova específica de uma falha dentro da ArmoredIA seguida de Recovery direto na própria IA.

---

# 19. Auditoria real de startup e estado persistente

Resumo observado:

~~~
120 itens inventariados
76 publicados
44 pendentes
0 falhos
76 limpos
120 workspaces
0 órfãos
76 publicações confirmadas
5 ambíguas
~~~

Publicações históricas ambíguas foram verificadas conforme o contrato do Hub.

## 19.1 Snapshot formal observado em 30/09/2026 após reinício

Depois da interrupção causada pela perda real de internet, o processo foi reiniciado. O Startup Audit encontrou:

~~~
itens                   = 209
publicados              = 128
pendentes               = 81
falhos                  = 0
limpos                  = 128
workspaces              = 209
órfãos                  = 0
publicações_confirmadas = 128
ambíguas                = 0
~~~

Esse snapshot comprova que o SQLite preservou o estado de 209 itens, sem órfãos e sem transformar a interrupção de rede em uma massa de itens FAILED.

## 19.2 Snapshot operacional externo usado para o teste

Antes da execução histórica zerada, o tópico do Hub foi limpo exclusivamente por script:

~~~
Grupo Hub          : -1004341972306
Tópico             : 228
Mensagens antes    : 79
Mensagens apagadas : 79
Tópico raiz        : preservado
Grupo fonte        : não tocado
~~~

Esse procedimento criou um destino externo limpo sem apagar o histórico da fonte Telegram.

---

# 20. Teste histórico do zero — execução real

O teste histórico foi iniciado com o estado interno zerado e o histórico Telegram preservado.

## 20.1 O que foi zerado

~~~
storage/database/armoredcreator.db
items = 0
publications = 0
checkpoints = 0
mode = CATCH_UP

storage/videos
vazio

storage/logs
vazio

storage/backups
vazio, preservando .gitkeep
~~~

## 20.2 O que não foi apagado

~~~
credentials/project.env
credentials/telegram/session/*
ArmoredStudio/runtime/rvc/*
modelos RVC
START_ALL.local-backup.bat
armoredcreator.db na raiz do projeto
~~~

O banco canônico é o que fica em storage/database/armoredcreator.db.

## 20.3 Resultado observado

~~~
CATCH-UP
-> descobrir histórico
-> um candidato
-> materializar
-> Vision
-> ArmoredIA
-> Studio
-> Hub
-> CONFIRMED
-> PUBLISHED
-> cleanup
-> próximo candidato
~~~

## 20.4 Falha real de conectividade observada

Durante o processamento do item 697, a internet caiu.

~~~
697
-> download iniciado
-> 1.0 MiB de 42.2 MiB
-> conexão Telegram encerrada
-> 60s sem progresso
-> materialização interrompida
-> checkpoint não avança
~~~

O Coordinator não marcou 697 como PUBLISHED nem como FAILED definitivo. Entretanto, depois das tentativas de reconexão, o processo encerrou com código 1 porque o Telegram continuava indisponível.

Esse caso comprova checkpoint seguro diante da falha, mas deixa aberta a resiliência de manter o processo vivo até a conectividade retornar.

## 20.5 Regra de contagem do CATCH-UP

Para fins operacionais, os estados são contabilizados por quantidade, não por exemplos de IDs:

~~~
PUBLICADOS
-> quantidade de itens cuja pipeline foi concluída
-> Telegram CONFIRMED
-> cleanup concluído
-> checkpoint pode avançar

RECOVERY
-> quantidade de itens com falha técnica/processamento incompleto
-> precisa de recuperação
-> checkpoint permanece bloqueado até resolução

WAITING_VISION
-> quantidade de itens para os quais a Vision não encontrou produto Shopee
-> sem destino de publicação por enquanto
-> permanece no SQLite
-> não é erro
-> não é Recovery
-> não bloqueia CATCH-UP
-> não impede LIVE
~~~

## 20.6 O que observar para a certificação final

- um item ativo por vez;
- nenhum pré-download de lote;
- nenhum estado de fila física;
- WAITING_VISION somente para unresolved V1;
- RECOVERY para falhas técnicas;
- continuidade do histórico após falha;
- Recovery do estágio correto;
- checkpoint nunca saltando bloqueio;
- nenhuma republicação indevida;
- cleanup somente após confirmação;
- zero órfãos;
- CATCH-UP completo;
- entrada correta em LIVE.

---

# 21. CATCH-UP -> LIVE

Chegar ao fim do iterador não basta.

**Estado atual:** ainda não observado em execução real desde o teste histórico zerado.

Condição:

~~~
histórico esgotado
+ nenhum bloqueio
+ itens elegíveis resolvidos
+ publicações confirmadas
+ cleanup concluído
+ zero órfãos
-> LIVE
~~~

Recovery pendente não pode ser ignorado para entrar em LIVE.

---

# 22. Vision V2

Vision V2 está fora desta versão.

Não fazem parte do fluxo atual discovery de equivalentes, pacote de múltiplos links ou CandidateDiscovery.

Uma futura V2 deve voltar isoladamente, com nova certificação.

---

# 23. Critérios de fechamento histórico

O freeze da versão atual depende de evidência, não apenas da existência de testes unitários.

~~~
[ ] Selector compara todas as candidatas válidas
[ ] ranking/score local é determinístico e testado
[ ] primeira candidata válida não é mais escolhida por ordem de chegada

[ ] cada candidata do batch é persistida no SQLite
[ ] motivo de cada rejeição é persistido
[ ] score é persistido
[ ] candidata selecionada é identificável no histórico

[ ] Hub Preflight ocorre antes do Studio/RVC
[ ] configuração inválida do Hub não consome processamento caro

[ ] perda temporária de internet não encerra Coordinator
[ ] Sync reconecta progressivamente enquanto Telegram estiver indisponível
[ ] candidato interrompido por falha de rede é reencontrado
[ ] checkpoint continua seguro durante a recuperação

[ ] falha real da ArmoredIA gera RECOVERY
[ ] Recovery da ArmoredIA volta diretamente para IA
[ ] Vision não é repetida nesse caso

[ ] CATCH-UP histórico completo desde checkpoint zero
[ ] nenhum candidato legítimo ficou para trás
[ ] nenhum predecessor foi pulado
[ ] checkpoints finais conferidos

[ ] WAITING_VISION ocorre somente para unresolved real da Vision V1
[ ] WAITING_VISION não bloqueia CATCH-UP
[ ] WAITING_VISION não impede LIVE
[ ] WAITING_VISION permanece persistido no SQLite
[ ] nenhum erro técnico de IA/Studio/Hub é mascarado como WAITING_VISION

[ ] Studio e RVC completos
[ ] Hub idempotente
[ ] CONFIRMED com message_id real
[ ] UNKNOWN não republica automaticamente
[ ] cleanup somente após PUBLISHED
[ ] zero órfãos

[ ] CATCH-UP concluído
[ ] nenhum Recovery/bloqueio pendente impede a transição
[ ] modo LIVE ativado
[ ] primeiro item LIVE processado
[ ] confirmação Telegram real em LIVE

[ ] suíte local final verde
[ ] CI final verde no commit de freeze
[ ] README atualizado com evidências finais
[ ] versão congelada/tagueada
~~~

---

# 24. Estado de certificação

## Fechado por contrato e testes

~~~
Coordinator
SQLite
Storage
Recovery
Startup Audit
Sync
Telegram lifecycle
Vision V1
ArmoredIA
Caption Policy
Studio
RVC
Hub
reconciliação
idempotência
cleanup
~~~

## Comprovado em execução real nesta fase

~~~
Vision -> ArmoredIA
Gemini real
Studio/RVC real
RVC voice=melody
Hub real
Telegram CONFIRMED
PUBLISHED + cleanup
Recovery real
restart com SQLite persistente
checkpoint preservado após falha de materialização
0 órfãos no Startup Audit observado
CI verde: 160 passed, 1 skipped
~~~

## Comprovado parcialmente

~~~
Recovery retomando de state=IA após restart
indisponibilidade temporária da internet durante download
reconexão Telegram após perda de conectividade
histórico real com dezenas de itens consecutivos
WAITING_VISION classificado e persistido como estado sem destino
WAITING_VISION liberando progresso do CATCH-UP: coberto por teste regressivo automatizado
~~~

## Evidência ainda necessária / pendência de implementação

~~~
Selector: ranking da melhor entre todas as válidas
Persistência individual das candidatas + rejeições + score
Hub Preflight antes do Studio/RVC
Coordinator permanecer vivo durante indisponibilidade temporária Telegram
falha real de ArmoredIA -> RECOVERY -> IA
CATCH-UP histórico completamente esgotado
checkpoints finais de todos os tópicos do histórico
transição final CATCH-UP -> LIVE
primeiro item LIVE real após o CATCH-UP
suíte local final após todas as correções de fechamento
~~~

---

# 25. Regra operacional final

~~~
SYNC
-> descobre e materializa

VISION V1
-> identifica
-> persiste contexto
-> unresolved = WAITING_VISION

ARMOREDIA
-> até 10 candidatas
-> Policy local
-> todas as válidas
-> ranking local determinístico
-> melhor candidata
-> exaustão = RECOVERY
-> falha técnica = RECOVERY
-> não reroda Vision

HUB PREFLIGHT
-> valida configuração essencial
-> bloqueia trabalho caro quando configuração é impossível

STUDIO
-> análise
-> RVC
-> FFmpeg

HUB
-> publish_once
-> reconciliação
-> CONFIRMED / ABSENT / UNKNOWN
-> UNKNOWN não republica automaticamente

CLEANUP
-> somente após PUBLISHED

CATCH-UP
-> um por vez
-> checkpoint seguro
-> Recovery posterior

LIVE
-> somente após histórico seguro
~~~

O objetivo da certificação é provar que cada efeito externo, cada mudança de estado e cada avanço de checkpoint possui evidência persistente e recuperável.

---

# 26. Escopo bloqueado antes do próximo grupo fonte

A versão atual deve ser tratada como **Fonte 1 em certificação**.

O grupo fonte atual permanece:

~~~
ARMORED_SYNC_SOURCE=-1003788989075
ARMORED_SYNC_SOURCE_ID=-1003788989075
~~~

Qualquer segundo grupo fonte será **aditivo** e jamais substituirá a Fonte 1.

Antes de implementar a Fonte 2, o ciclo da Fonte 1 deve concluir a certificação histórica desta versão, incluindo CATCH-UP completo e a transição para LIVE.

A Fonte 2 deverá ter seus próprios parâmetros e checkpoints sem romper o princípio global de um único item ativo. O comportamento do Sync atual de descobrir os tópicos do fórum também deve continuar explícito: ARMORED_SYNC_TOPIC_NAME não é hoje um filtro exclusivo.

---

# 27. Regra de manutenção

O baseline oficial continua intocado.

Qualquer mudança futura deve ser isolada em branch própria, passar pela suíte completa, passar por git diff --check e obter evidência operacional antes de ser tratada como fechada.