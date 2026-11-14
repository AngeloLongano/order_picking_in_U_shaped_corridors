# Benchmark CP-SAT e Gurobi

Il benchmark confronta i due solver sul problema di picking con deposito mobile. Valuta le stesse istanze con ricerche **Basic** (griglia uniforme) e **ZoomIn** (griglie successive attorno al miglior deposito). Per ogni posizione del deposito risolve il problema dei giri del picker. L'unità di lavoro è `istanza × solver × metodo × dimensione`; i processi possono eseguire lavori diversi in parallelo, mentre le scansioni di un singolo ZoomIn restano sequenziali.

La campagna legge le istanze da `instances/PRP-UW-instances.txt`. Il confronto principale usa in entrambi i solver coefficienti interi `round(distanza_euclidea_m × 10_000)` per gli archi. La scelta del deposito usa invece il **costo totale ricalcolato**: distanza euclidea dei giri più `ν × distanza(ingresso, deposito)`. L'obiettivo del solver, la distanza euclidea e il costo totale sono conservati separatamente. Il profilo euclideo opzionale di Gurobi è distinto dal confronto principale.

## Avvio rapido

Dalla radice del progetto, con Python 3.13, `uv` e una licenza Gurobi valida:

```sh
uv sync
uv run python -m src.benchmark preview
uv run python -m src.benchmark run
```

La CLI accetta un solo argomento posizionale: `preview` oppure `run`. Non si passa il nome dello scenario alla CLI: si sceglie con `active` in `benchmarks/scenari.yaml`. **`preview` non avvia solver e non modifica risultati o stime**; valida la configurazione e mostra lavori completi, mancanti, errori da ritentare, intervallo dei candidati e durate stimate. `run` esegue i lavori mancanti e salva i risultati. Ripetere lo stesso comando `run` riprende la serie dai checkpoint.

Le durate stimate sono mostrate in minuti e secondi, per esempio `95 min 0.7 s`.

## Scegliere la campagna

Aprire `benchmarks/scenari.yaml` e impostare, per esempio, `active: tecnica`. Prima di ogni campagna eseguire `preview`, poi `run`.

| `active` | Istanze | Metodi e dimensioni | Uso |
| --- | ---: | --- | --- |
| `tecnica` | 1 | Basic 1D | Verifica rapida di CLI, solver, licenza e checkpoint. |
| `pilota` | 36 | Basic 1D/2D, ZoomIn 1D/2D | Misura iniziale rappresentativa; due solver per ogni configurazione. |
| `basic_2d` | 120 | Basic 2D | Confronto completo sulla stessa griglia di depositi. |
| `completo` | 120 | Basic 1D/2D, ZoomIn 1D/2D | Confronto di tutti i metodi. |

Il pilota contiene tre istanze per ciascuno dei 12 gruppi layout × numero di prelievi: difficoltà prevista facile, media e difficile. La scelta usa domanda totale, limite inferiore al numero di giri e dispersione spaziale dei prelievi, normalizzati entro ogni gruppo; indicatori e nomi sono registrati nel file YAML. `instances: all` espande le 120 istanze in ordine stabile. Per una campagna personalizzata aggiungere la stessa chiave in `scenari.yaml` e `opzioni.yaml`, specificando un elenco di nomi o `all`.

## Configurazione e confronto corretto

`benchmarks/opzioni.yaml` contiene opzioni comuni, metodi per scenario e passi delle griglie. I valori iniziali sono capacità `15`, peso del muletto `1/3`, limite `0,5 s` per deposito, scala obiettivo `10_000`, un thread per solver e due processi per le campagne principali. `processes` indica quanti lavori indipendenti eseguire insieme; `solver_threads` indica i thread usati *dentro* ciascun solver. I parametri comuni, i passi e il limite possono essere sovrascritti nello scenario: una modifica produce una nuova serie.

Per Basic, i due solver visitano gli stessi depositi: confrontare i candidati per istanza e coordinate. Per ZoomIn, la scelta del miglior deposito può produrre griglie successive diverse: confrontare i migliori risultati per istanza e configurazione; un confronto per deposito usa solo le posizioni visitate da entrambi. I tempi dei solver sono più interpretabili in una serie sequenziale, perché la contesa fra processi può influire su durate e stati. Per misurare il throughput si possono creare scenari distinti con `processes: 1`, `2` e, dopo verifica della licenza e della memoria, `4`.

`OPTIMAL` indica una prova di ottimalità del solver per il **modello a deposito fisso**, con controllo aggiuntivo sulla distanza fra obiettivo e bound. `FEASIBLE` indica un giro valido senza quella prova; include un timeout con soluzione. `TIME_LIMIT_NO_SOLUTION` indica un timeout senza soluzione e `ERROR` un errore. Quando manca una soluzione, obiettivo, distanze e costo restano vuoti. I giri con soluzione vengono controllati per prelievi unici, capacità, archi e costi. La ricerca ZoomIn resta euristica rispetto alla scelta globale del deposito, anche se un singolo giro è `OPTIMAL`.

## Risultati, checkpoint e stime

Ogni serie è in `results/<run_id>/`. L'identificativo dipende da istanze, parametri, dataset e codice: cambiare uno di questi elementi crea una serie distinta. La directory contiene:

| File | Contenuto |
| --- | --- |
| `config.json` | Configurazione congelata, hash, versioni, piattaforma e tempi complessivi. |
| `candidati.csv` | Una riga per deposito valutato: coordinate, stato, obiettivo, bound, gap, costi, tempi e parametri del solver. |
| `riepilogo.csv` | Una riga per lavoro: conteggi degli stati, miglior deposito e durata. |
| `checkpoints/` | Un file atomico per candidato e un riepilogo di completamento per lavoro. |
| `attempts/` | Errori dei lavori e tentativi successivi. |

I processi scrivono i propri checkpoint; un solo coordinatore rigenera i CSV senza append concorrenti. In caso di interruzione, `run` riusa i candidati completati. Per ZoomIn ricostruisce nell'ordine il miglior deposito di ogni scansione prima di procedere. Un lavoro fallito viene ritentato e non aggiorna le stime finché non termina normalmente.

`benchmarks/stime_init.json` contiene prior per la preview, estrapolati dalle tabelle del paper di Montemanni, Cervino e Lolli: **non sono misure locali**, né misure dirette di Gurobi. `benchmarks/stime.json` contiene le stime correnti e lo storico degli aggiornamenti. La prima durata locale sostituisce il prior; le successive usano la media dei valori vecchio e nuovo, oppure la sostituzione se `estimates.update: replace`. La chiave include istanza, solver, metodo, dimensione e profilo dei parametri, ma non il nome della campagna. Per ripristinare i prior, aumentare `estimates.reset_generation` in `opzioni.yaml`: `preview` segnala il reset previsto e il primo `run` della nuova generazione lo applica una sola volta.

## Analisi e ordine delle fasi

Aprire `notebooks/benchmark_picking.ipynb` con il kernel Python della `.venv`. Il notebook legge soltanto le serie già salvate e mostra quote di soluzioni e ottimi, errori, timeout, tempi, depositi comuni e differenze dei costi. Le quote per candidato usano i candidati tentati come denominatore; i costi sono confrontati solo quando entrambi i solver hanno una soluzione. La durata totale della serie viene da `config.json`, non dalla somma delle durate dei lavori paralleli.

L'ordine previsto è: **prova tecnica → pilota → Basic 2D completo → confronto completo → analisi**. Dopo il pilota, controllare validità dei giri e dei costi, ripresa senza duplicati, licenza, memoria, quota con soluzione e prestazioni con diversi numeri di processi. Se `0,5 s` produce molti `FEASIBLE` o timeout senza soluzione, fissare prima delle campagne complete un eventuale secondo limite (per esempio `3 s`) e salvarlo come serie distinta. Passare al confronto completo dopo aver verificato che uno ZoomIn ripreso restituisca lo stesso risultato di una run continua.

Per le scelte sperimentali e le fonti: [Montemanni, Cervino e Lolli, tabelle 1–2](https://iris.unimore.it/retrieve/4c9fefc1-5590-4b12-993e-d1a9ad5e77d7/Final.pdf) e [Diefenbach et al.](https://link.springer.com/article/10.1007/s00291-021-00663-8).
