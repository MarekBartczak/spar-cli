# SPAR CLI — zewnętrzna ocena projektu i mapa dalszego rozwoju

> **Stan oceny:** 10 lipca 2026  
> **Repozytorium:** `MarekBartczak/spar-cli`  
> **Przeglądany branch:** `master`  
> **Aktualny commit widoczny podczas oceny:** `eedf829`  
> **Cel dokumentu:** przekazanie niezależnej oceny do dalszej pracy z Claude/Fable nad projektem.

---

## Instrukcja dla agenta pracującego nad repozytorium

Traktuj ten dokument jako **zewnętrzny przegląd i listę hipotez do zweryfikowania**, a nie jako gotowy backlog do bezrefleksyjnego wdrożenia.

Przed zaplanowaniem zmian:

1. porównaj każdą uwagę z aktualnym kodem, `README.md`, `CONTEXT.md`, `docs/HANDOFF.md` i ADR-ami;
2. oznacz punkty jako:
   - już zaimplementowane,
   - częściowo zaimplementowane,
   - zaplanowane,
   - nieaktualne,
   - rzeczywiście brakujące;
3. nie rozpoczynaj dużego refaktoru tylko dlatego, że moduł jest długi;
4. priorytetem pozostaje utwardzanie prawdziwych runów i bezpieczeństwo pracy użytkownika;
5. propozycje dotyczące wydania, CI i społeczności traktuj jako późniejszy etap, nie jako blokadę bieżącego developmentu.

---

# 1. Kontekst oceny

SPAR nie jest obecnie oceniany jako produkt gotowy do publikacji. Projekt jest w fazie intensywnego dogfoodingu:

- dopiero niedawno przeszedł pełny happy path;
- kolejne realne runy ujawniają problemy, które są następnie zamieniane w poprawki i testy regresyjne;
- testy są uruchamiane lokalnie;
- GitHub Actions zostało świadomie odłożone, między innymi ze względu na koszty;
- docelowym celem jest dopracowane narzędzie open source, a nie szybka monetyzacja;
- publiczne wydanie ma nastąpić dopiero wtedy, gdy narzędzie będzie bezpieczne i przewidywalne dla użytkownika spoza projektu.

W tym kontekście brak release pipeline’u, PyPI czy pełnego CI **nie jest obecnie istotną wadą**. Ważniejsza jest jakość fundamentu, poprawność state machine, recovery, operacji Git i kontraktu pomiędzy agentami.

---

# 2. Ocena wykonawcza

## Werdykt

**SPAR ma bardzo mocny fundament i realny wyróżnik. Nie widać fundamentalnej luki koncepcyjnej, która podważałaby sens projektu.**

Projekt nie jest jedynie wrapperem uruchamiającym dwa modele. Jego właściwym produktem jest protokół inżynierski:

- dwie niezależne strony od różnych dostawców;
- wspólny, wersjonowany artefakt;
- strukturalne werdykty zamiast zaufania do swobodnej wypowiedzi modelu;
- konsensus oparty na stanie artefaktu i braku blokujących uwag;
- rozbicie wykonania na zadania z zależnościami i zakresem plików;
- osobne branche i worktree;
- implementacja przez jedną stronę i review przez drugą;
- testy jako obiektywne bramki;
- zapisany stan, wznowienie i bramki użytkownika;
- tryb headless dla host-agenta oraz GUI do pilotowania i obserwacji.

To jest właściwy kierunek dla narzędzia agentic engineering. Wartość SPAR-a nie polega na twierdzeniu, że „dwa modele zawsze są mądrzejsze”, tylko na tym, że **żaden model nie jest samodzielnym źródłem prawdy**.

## Ocena dopasowana do aktualnego etapu

| Obszar | Ocena | Komentarz |
|---|---:|---|
| Pomysł i wyróżnik | 9/10 | Jasny, praktyczny problem i odróżnienie od prostych multi-agent wrapperów |
| Protokół debaty i review | 8.5/10 | Strukturalne werdykty, remark ledger, niezależne role i budżety rund |
| State machine i recovery | 8/10 | Dużo przemyślanych mechanizmów; nadal najważniejszy obszar hartowania |
| Git/worktree isolation | 8/10 | Dobry fundament, realne testy ujawniły i domykają edge case’y |
| Testowanie na obecnym etapie | 8.5/10 | Szeroka lokalna suite, E2E, fake CLI i opcjonalne testy kontraktowe |
| UX CLI/headless/GUI | 8/10 | Rzadko spotykane połączenie interaktywności, automatyzacji i obserwowalności |
| Architektura kodu | 7.5/10 | Logiczny podział domenowy, ale dwa główne moduły zaczynają skupiać zbyt wiele odpowiedzialności |
| Dokumentacja techniczna | 7.5/10 | README, glossary, AGENT i ADR-y są mocne; występuje jednak drift decyzji |
| Security/trust model | 6.5/10 | Mechanizmy ochronne istnieją, lecz przed publicznym wydaniem wymagają jawnego kontraktu bezpieczeństwa |
| Gotowość do publicznego wydania | 4/10 | Zgodnie z założeniem: to jeszcze nie jest cel obecnego etapu |

**Ocena fundamentu na obecnym etapie: około 8.5/10.**

Niska ocena gotowości wydawniczej nie obniża oceny projektu jako aktywnie rozwijanego narzędzia. Oznacza tylko, że przed przekazaniem go obcym użytkownikom konieczne będzie dalsze hartowanie.

---

# 3. Najmocniejsze elementy projektu

## 3.1. SPAR ma rzeczywistą tezę produktową

Teza jest czytelna:

> Plan, implementacja i review nie powinny zależeć od opinii jednego modelu ani od luźnej rozmowy pomiędzy modelami. Proces powinien być kontrolowany przez deterministyczny orkiestrator, Git, testy i jawne bramki.

To jest znacznie mocniejsze niż „uruchom Claude, potem poproś Codexa o opinię”.

SPAR formalizuje ręczny workflow, który zaawansowani użytkownicy agentów już wykonują:

1. jeden model tworzy plan;
2. drugi go podważa;
3. uwagi są rozwiązywane;
4. plan zostaje rozbity na zadania;
5. jedna strona implementuje;
6. druga recenzuje;
7. rzeczywiste testy decydują o merge’u;
8. użytkownik zachowuje ostateczną kontrolę.

## 3.2. Dobra separacja deklaracji modelu od dowodu

Model może zadeklarować, że:

- zadanie jest ukończone;
- testy przeszły;
- uwaga została rozwiązana;
- implementacja jest poprawna.

SPAR słusznie nie powinien uznawać tych stwierdzeń za dowód. Dowodem są:

- zapisany artefakt;
- hash;
- diff;
- stan repozytorium;
- parser werdyktu;
- kod wyjścia testu;
- konkretna gałąź i commit;
- zatwierdzenie użytkownika.

Ten kierunek jest jednym z najważniejszych atutów projektu.

## 3.3. Cross-vendor review ma sens

Zasada, że implementer i reviewer są po przeciwnych stronach, jest dobra. Ogranicza ryzyko, że ten sam agent:

- uzasadni własne decyzje;
- przeoczy własne założenia;
- zaakceptuje błąd wynikający z tego samego sposobu rozumowania;
- zmieni wymagania tak, aby dopasować je do implementacji.

Dodatkowo reviewer działa w trybie read-only, co wzmacnia rozdział odpowiedzialności.

## 3.4. Izolacja zadań przez Git i worktree

Osobny branch i worktree dla zadania to kosztowniejsze rozwiązanie niż zwykłe modyfikowanie jednego drzewa roboczego, ale daje dużo korzyści:

- kontrolowany zakres zmian;
- możliwość niezależnego review;
- jasny punkt bazowy;
- łatwiejszy rollback;
- test zadania przed integracją;
- możliwość wiązania review z konkretną wersją kodu;
- ograniczenie wzajemnego zanieczyszczania się tasków.

Realne runy już pokazały, że ta izolacja ujawnia problemy planowania, np. brak zależności do plików tworzonych przez późniejsze zadania. To dobra cecha systemu, nie wada.

## 3.5. Headless mode jest bardzo wartościowy

Kontrakt:

- zapis stanu;
- wyjście kodem `10`;
- `spar status --json`;
- wznowienie przez `--gate`;
- oddzielenie verbose streamu od kontekstu host-agenta;

jest dobrze dopasowany do automatyzacji przez Claude Code, Codex lub innego nadrzędnego agenta.

To może być jeden z najmocniejszych wyróżników SPAR-a. Narzędzie nie próbuje udawać samodzielnego inteligentnego asystenta. Działa jako silnik procesu, którym może sterować host.

## 3.6. Rozwój przez realne awarie

`docs/HANDOFF.md` pokazuje bardzo zdrowy sposób pracy:

1. prawdziwy run;
2. wykrycie problemu;
3. poprawka mechanizmu;
4. test regresyjny;
5. ponowny run;
6. kolejna iteracja.

Wśród problemów znalezionych na żywo były m.in.:

- błędne rozwiązywanie ścieżki worktree;
- scope guard reagujący na artefakty builda;
- pozostawiony integration branch;
- spór implementer–reviewer prowadzący do złego abortu;
- niewłaściwy prefix review;
- brakujące informacje dla reviewera o plikach z innych tasków.

To jest dokładnie materiał, którego nie da się w pełni wymyślić przy biurku.

## 3.7. Testy są szerokie jak na etap alpha

Repo zawiera testy dotyczące m.in.:

- adapterów Claude i Codex;
- streamingu subprocessów;
- parsera konfiguracji;
- CLI;
- parsera task listy;
- Git operations;
- stanu debaty i execution;
- guardów;
- bramek;
- orchestratora;
- review loop;
- E2E debaty;
- E2E wykonania;
- opcjonalnych testów kontraktowych z prawdziwymi CLI;
- GUI i jego state machine.

Szczególnie wartościowe są testy korzystające z prawdziwych tymczasowych repozytoriów Git i kontrolowanych fake CLI. To daje znacznie więcej niż same testy metod i mocków.

---

# 4. Najważniejsza decyzja strategiczna: granica produktu

W aktualnej historii projektu występuje ważne napięcie.

ADR `0003` przyjmuje decyzję:

> SPAR jest silnikiem obsługiwanym przez host-agenta, a nie samodzielnym asystentem. Grill wymagań i TUI zostają porzucone, aby nie duplikować możliwości Claude Code lub Codexa.

Następnie projekt otrzymał pełne `spar gui`, a bieżący roadmap ponownie rozważa:

- grill-with-docs wewnątrz GUI;
- chat pane;
- potencjalnie embedded terminal;
- szerszą rolę GUI jako miejsca rozpoczęcia całego procesu.

Oba kierunki są poprawne, ale prowadzą do dwóch różnych produktów.

## Wariant A — engine first

SPAR pozostaje:

- deterministycznym silnikiem debaty, wykonania i review;
- narzędziem uruchamianym przez host-agenta lub bezpośrednio z CLI;
- GUI jest dashboardem/pilotem, ale nie przejmuje roli ogólnego asystenta;
- wymagania są dostarczane przez użytkownika lub zewnętrzny agent.

Zalety:

- mniejszy zakres;
- łatwiejsza stabilizacja;
- mniej zależności od konkretnego UI;
- prostszy model bezpieczeństwa;
- mocniejsza tożsamość jako infrastruktura agentic engineering;
- łatwiejsze kontrybucje do niezależnych modułów.

## Wariant B — zintegrowane środowisko SPAR

SPAR staje się:

- miejscem rozmowy z użytkownikiem;
- narzędziem do grilla wymagań;
- panelem uruchamiania modeli;
- dashboardem;
- silnikiem debaty i wykonania;
- częściowo alternatywą dla host-agenta.

Zalety:

- lepszy onboarding dla użytkownika, który nie chce ręcznie sterować kilkoma CLI;
- bardziej spójny „produkt w jednym oknie”;
- większa kontrola nad UX.

Koszty:

- dużo większa powierzchnia produktu;
- utrzymywanie czatu, sesji, renderowania, terminala, przerwań i modeli;
- ryzyko dublowania Claude Code/Codexa;
- trudniejsza kompatybilność między systemami;
- mniej czasu na hartowanie rdzenia;
- potencjalnie rozmyta tożsamość projektu.

## Rekomendacja

Przed wdrożeniem grilla i embedded terminala warto formalnie odpowiedzieć:

> Czy GUI ma być tylko pilotem silnika SPAR, czy ma stać się pełnym miejscem interakcji z agentami?

Rekomendowany kierunek na najbliższy etap:

- zachować **engine first**;
- GUI rozwijać jako dashboard/pilot;
- grill dodać dopiero po utwardzeniu rdzenia, jako opcjonalny moduł;
- oznaczyć ADR `0003` jako `Amended` albo dodać nowe ADR opisujące zmianę decyzji;
- nie pozwolić, aby rozwój GUI wyprzedził niezawodność recovery i Git lifecycle.

To nie oznacza, że grill jest złym pomysłem. Oznacza tylko, że jest nowym zakresem produktu, a nie drobną funkcją GUI.

---

# 5. Priorytety techniczne — etap bieżący

## P0: wymagane podczas dalszego dogfoodingu

### 5.1. Macierz awarii i wznowień

Największym ryzykiem nie jest już happy path. Jest nim stan po przerwaniu w dowolnym miejscu procesu.

Należy systematycznie przetestować przerwanie:

- przed wywołaniem modelu;
- po zapisaniu `turn_in_progress`;
- podczas streamingu;
- po zmianie plików, ale przed werdyktem;
- po werdykcie, ale przed zapisem stanu;
- po commicie implementera;
- podczas review;
- po review, ale przed testem;
- podczas testu;
- po teście, ale przed merge’em;
- podczas merge’a;
- po merge’u taska, ale przed oznaczeniem go jako zakończony;
- podczas final testu;
- podczas final merge’a;
- podczas czyszczenia worktree i branchy.

Dla każdej fazy powinno być wiadomo:

- czy operacja jest idempotentna;
- czy zostanie automatycznie dokończona;
- czy zostanie bezpiecznie powtórzona;
- czy wymaga decyzji użytkownika;
- czy SPAR potrafi rozpoznać już wykonany krok;
- czy nie powstanie drugi commit, drugi merge albo utrata zmian.

### 5.2. Formalne inwarianty stanu

Warto posiadać jedno miejsce, które waliduje semantyczną poprawność stanu względem repozytorium.

Przykładowe inwarianty:

- task `merged` ma commit osiągalny z integration branch;
- review `DONE` dotyczy aktualnego commita taska;
- test `passed` dotyczy wersji kodu, która nie zmieniła się od testu;
- `turn_in_progress` wskazuje istniejącą stronę i fazę;
- `pending_gate` odpowiada rzeczywistemu miejscu state machine;
- worktree wskazane w stanie istnieje albo recovery wie, jak je odbudować;
- target branch nie został potajemnie przesunięty;
- integration branch ma oczekiwanego rodzica;
- nie istnieje task oznaczony jako zakończony bez wymaganych danych;
- stan `done` nie pozostawia aktywnych artefaktów, które blokują nowy run.

Sugerowany model:

```text
load state
→ validate schema
→ inspect repository
→ reconcile known safe differences
→ validate semantic invariants
→ continue or emit explicit recovery gate
```

Walidacja powinna być uruchamiana przynajmniej:

- po odczycie stanu;
- przed wznowieniem;
- przed krytycznym merge’em;
- po krytycznym merge’u;
- przed oznaczeniem runu jako zakończony.

### 5.3. Powiązanie decyzji z konkretną wersją kodu

Należy dopilnować, aby żaden wynik nie był wykorzystywany po zmianie ocenianego obiektu.

Review powinno być związane co najmniej z:

- task ID;
- base commit;
- reviewed commit;
- hashem diffu lub drzewa;
- wersją planu;
- modelem i stroną reviewera;
- wersją protokołu.

Analogicznie wynik testu:

- command;
- cwd/worktree;
- commit/tree hash;
- exit code;
- czas;
- ewentualnie skrót outputu.

Po jakiejkolwiek zmianie kodu:

- stare review staje się nieaktualne;
- stary wynik testu nie może bramkować merge’a;
- GUI/status nie powinny pokazywać ich jako obowiązujących.

Część tego może już istnieć. Punkt wymaga audytu, nie automatycznie nowej implementacji.

### 5.4. Domknięcie znanych luk recovery

`docs/HANDOFF.md` wymienia kilka konkretnych scenariuszy, które powinny zostać sprawdzone zanim dojdą kolejne duże funkcje:

- merge conflict w headless mode na zaawansowanym target branchu;
- wznowienie po nieudanym per-task teście;
- odbudowanie worktree przed sprawdzeniem abortu, pozostawiające śmieci;
- `--gate` użyte przeciwko stanowi `done`;
- porzucone runy i cleanup branchy/worktree;
- zachowanie po ręcznej zmianie repozytorium w trakcie runu.

To jest obecnie bardziej wartościowe niż dodawanie kolejnego providera.

### 5.5. Polityka zmian wykonywanych równolegle przez użytkownika

Trzeba jasno zdefiniować, co dzieje się, gdy podczas aktywnego runu użytkownik:

- zmieni target branch;
- doda commit do target branch;
- ręcznie edytuje worktree SPAR-a;
- usunie branch;
- wykona rebase;
- usunie katalog worktree;
- uruchomi drugi proces;
- kliknie gate w GUI równocześnie z host-agentem.

Rekomendowana polityka:

- zamrozić `target_base_commit` na początku runu;
- nie wykonywać automatycznego rebase’u bez jawnej decyzji;
- wykrywać zmianę target branchu przed final merge’em;
- manualną ingerencję w worktree traktować jako osobny stan wymagający decyzji;
- zachować jeden proces decyzyjny dzięki lockowi;
- GUI otwarte na zablokowanym repo powinno pozostać read-only.

### 5.6. Fixture’y z prawdziwych zachowań modeli

Kontynuować proces:

```text
realny problem
→ zanonimizowany transcript/event stream
→ fixture
→ test regresyjny
→ poprawka
```

Szczególnie cenne przypadki:

- niepełny JSONL;
- eventy w innej kolejności;
- brak final eventu;
- kilka werdyktów;
- markdown wokół werdyktu;
- output po werdykcie;
- poprawny exit code bez oczekiwanego rezultatu;
- model deklarujący brak zmian, mimo istniejącego diffu;
- model commitujący samodzielnie;
- polecenie kończące się sukcesem po wcześniejszym błędzie;
- timeout z procesem potomnym pozostającym przy życiu;
- sesja CLI, której nie można wznowić.

---

# 6. Security i model zaufania

Przed publicznym wydaniem SPAR potrzebuje jawnego dokumentu `SECURITY_MODEL.md` albo rozdziału w dokumentacji.

## 6.1. Kod i polecenia generowane przez modele

SPAR:

- pozwala implementerowi modyfikować repozytorium;
- może pozwalać mu uruchamiać shell;
- wykonuje komendy testowe zapisane w planie;
- uruchamia zewnętrzne CLI;
- przechowuje transcript i output poleceń.

To jest zgodne z przeznaczeniem projektu, ale użytkownik musi wiedzieć, że:

> SPAR uruchamia nieufny, generowany przez modele kod i polecenia z uprawnieniami bieżącego użytkownika.

Przed publicznym alpha należy opisać:

- rekomendację pracy w repo bez produkcyjnych sekretów;
- możliwość uruchamiania w kontenerze/VM;
- brak gwarancji sandboxowania przez sam SPAR;
- zakres uprawnień implementera i reviewera;
- sposób wykonywania `test=`;
- ryzyko złośliwego lub błędnego kodu w analizowanym repo.

Opcjonalny tryb kontenerowy może pojawić się później. Dokumentacja modelu zaufania jest potrzebna wcześniej.

## 6.2. Transcript, `live.log` i sekrety

Logi mogą zawierać:

- fragmenty kodu;
- ścieżki;
- komendy;
- output testów;
- zmienne środowiskowe przypadkowo wypisane przez narzędzia;
- treść plików konfiguracyjnych;
- tokeny lub sekrety ujawnione przez repo albo subprocess.

Do audytu:

- czy `.spar/` jest zawsze ignorowane przez Git;
- jakie są uprawnienia tworzonych plików;
- czy debug bundle może wyciekać kod lub sekrety;
- czy istnieje polityka retencji transcriptów;
- czy użytkownik może je łatwo usunąć;
- czy przed zgłoszeniem issue można wykonać redakcję danych;
- czy GUI nie wyświetla przypadkowo poufnych danych w miejscach niewidocznych dla użytkownika.

## 6.3. Review read-only

Warto utrzymywać silną gwarancję:

- reviewer nie może modyfikować plików;
- reviewer nie może wykonywać poleceń zmieniających repo;
- wynik review jest deklaracją, lecz SPAR sam sprawdza brak zmian;
- naruszenie kończy rundę review i wymaga bezpiecznego rollbacku lub abortu.

Ta cecha powinna być częścią publicznego kontraktu projektu.

---

# 7. Architektura i utrzymywalność

## 7.1. Obecny podział domenowy jest sensowny

Struktura:

- `adapters/`;
- `orchestrator.py`;
- `state.py`;
- `guard.py`;
- `exec/loop.py`;
- `exec/review.py`;
- `exec/tasklist.py`;
- `exec/gitops.py`;
- `headless`;
- `status`;
- `stream/watch/ui`;
- `gui/`;

odpowiada rzeczywistym elementom domeny i jest czytelna.

Nie ma potrzeby wykonywania dużego refaktoru tylko po to, aby zmniejszyć liczbę linii.

## 7.2. Pojawia się jednak koncentracja odpowiedzialności

Aktualne rozmiary głównych modułów:

- `spar/orchestrator.py`: około 1158 linii;
- `spar/exec/loop.py`: około 1272 linii;
- `spar/exec/review.py`: około 680 linii.

To jeszcze nie jest automatycznie problem. Jest to jednak sygnał, że dalsze dokładanie recovery, gate’ów i wyjątków może zwiększać ryzyko regresji.

Rekomendacja:

- nie robić „big bang refactor”;
- podczas kolejnych zmian wyciągać stabilne, dobrze nazwane komponenty;
- oddzielić czystą logikę przejść stanu od efektów ubocznych;
- utrzymywać Git operations w cienkiej, testowalnej warstwie;
- wydzielać polityki recovery, gdy zaczną mieć własne rozgałęzienia;
- budować jawne typy wyników zamiast wielu luźnych kodów i wyjątków.

Możliwy docelowy kierunek:

```text
debate/
  machine.py
  turn.py
  recovery.py
  protocol.py

execution/
  machine.py
  task_runner.py
  review_runner.py
  test_runner.py
  recovery.py
  cleanup.py

core/
  events.py
  errors.py
  fingerprints.py
  invariants.py
```

To jest kierunek, nie obecny obowiązkowy backlog.

## 7.3. Taksonomia błędów

W miarę dojrzewania projektu warto posiadać stabilne klasy lub kody domenowe, np.:

- `PROVIDER_TIMEOUT`;
- `PROVIDER_SESSION_LOST`;
- `PROVIDER_PROTOCOL_ERROR`;
- `MODEL_OUTPUT_INVALID`;
- `ARTIFACT_MISSING`;
- `STATE_CORRUPTED`;
- `STATE_REPO_MISMATCH`;
- `WORKTREE_DIRTY`;
- `OUT_OF_SCOPE_CHANGE`;
- `EMPTY_IMPLEMENTATION`;
- `REVIEW_STALE`;
- `REVIEW_DISPUTE`;
- `TEST_FAILED`;
- `TEST_TIMEOUT`;
- `MERGE_CONFLICT`;
- `TARGET_MOVED`;
- `USER_ACTION_REQUIRED`.

Korzyści:

- czytelniejsze komunikaty;
- stabilniejszy `status --json`;
- łatwiejsza obsługa GUI;
- lepsze recovery;
- prostsze debug bundle;
- możliwość testowania konkretnych klas awarii.

Nie trzeba od razu zmieniać publicznych exit codes. Exit code może pozostać kategorią wysokiego poziomu, a dokładny reason znaleźć się w JSON-ie.

---

# 8. Dokumentacja — mocne strony i drift

## Mocne strony

Bardzo dobre są:

- README pokazujące cały pipeline;
- `CONTEXT.md` jako słownik domeny;
- `docs/AGENT.md` opisujące kontrakt host-agenta;
- ADR-y dokumentujące decyzje;
- `HANDOFF.md` zapisujące rzeczywisty stan prac i live findings;
- opis exit codes;
- jawne ograniczenia i zasady GUI;
- dokumentacja konfiguracji modeli.

To jest poziom dokumentacji wyższy niż w wielu projektach na podobnym etapie.

## Co wymaga uporządkowania przed publicznym alpha

### 8.1. ADR 0003 a obecne GUI

ADR mówi, że UI nie będzie utrzymywane, a repo zawiera rozbudowane GUI i roadmap ponownie rozważa grill.

Rozwiązanie:

- oznaczyć ADR jako `Amended`/`Superseded`;
- dodać nowy ADR wyjaśniający, że GUI jest dashboardem-pilotem;
- osobno zdecydować o grillu i terminalu.

### 8.2. `PLAN.md`

`PLAN.md` jest wartościowym dokumentem historycznym, ale nie powinien być mylony z aktualnym stanem produktu.

Możliwe rozwiązania:

- nagłówek: `Historical design document`;
- przeniesienie do `docs/history/`;
- wskazanie, że źródłami aktualnego kontraktu są README, ADR-y, AGENT i HANDOFF.

### 8.3. Jedno źródło bieżącego roadmapu

`HANDOFF.md` jest bardzo szczegółowy, ale z czasem może stać się długim dziennikiem.

Przed szerszym otwarciem projektu warto rozdzielić:

- `CHANGELOG.md` — co zostało wydane;
- `ROADMAP.md` — co jest planowane;
- `KNOWN_LIMITATIONS.md` — czego jeszcze nie wspieramy;
- `docs/HANDOFF.md` — bieżące robocze przekazanie dla agenta.

---

# 9. Co powinno znaleźć się przed publicznym alpha

Nie wszystko musi zostać wykonane teraz. Poniższa lista opisuje próg wejścia dla obcych użytkowników.

## 9.1. Kryteria niezawodności

- kilkadziesiąt pełnych runów;
- kilka niezależnych repozytoriów;
- co najmniej kilka języków/build systemów;
- przerwania w różnych fazach;
- kontrolowane timeouty;
- nieudane testy;
- review dispute;
- konflikt merge;
- przesunięty target branch;
- restart procesu;
- brak utraty pracy;
- jednoznaczny recovery lub jawny abort z instrukcją naprawy.

Nie musi istnieć gwarancja automatycznego naprawienia każdego stanu. Musi istnieć gwarancja, że SPAR:

- nie zgaduje;
- nie niszczy pracy;
- mówi użytkownikowi, co znalazł;
- podaje bezpieczne opcje.

## 9.2. `spar doctor`

Komenda diagnostyczna powinna sprawdzać:

- wersję Pythona;
- wersję Git;
- czy katalog jest repo;
- czystość wymaganych branchy;
- dostępność `claude` i `codex`;
- podstawowe wywołanie obu CLI;
- konfigurację modeli;
- możliwość utworzenia worktree;
- wspierany system operacyjny;
- obecność pozostałości po starym runie;
- możliwość zapisu do `.spar`;
- opcjonalną zależność GUI.

To znacznie obniży koszt pierwszego uruchomienia i liczbę zgłoszeń niebędących błędami SPAR-a.

## 9.3. `spar debug-bundle`

Przydatny dla open source będzie bundle zawierający:

- wersję SPAR-a;
- wersję Pythona, Git i systemu;
- config po redakcji;
- stan runu;
- listę branchy i worktree;
- exit reason;
- kody wyjścia subprocessów;
- event metadata;
- opcjonalnie transcript po świadomym potwierdzeniu.

Domyślnie bundle nie powinien zawierać:

- pełnego kodu repo;
- tokenów;
- sekretów;
- całych plików `.env`;
- nieograniczonych transcriptów.

## 9.4. Wersjonowanie stanu i protokołu

Stan powinien zawierać jawnie:

```json
{
  "state_schema_version": 1,
  "protocol_version": 1,
  "spar_version": "0.x.y"
}
```

Przed wydaniem trzeba zdecydować:

- czy stare runy są migrowane;
- czy są odrzucane z jasnym komunikatem;
- jak długo wspierany jest zapisany stan;
- czy minor release może zmienić format;
- jak wygląda backup przed migracją.

## 9.5. Security model i wspierane platformy

README powinno jasno powiedzieć:

- jakie systemy są wspierane;
- czy Windows działa natywnie, przez WSL czy wcale;
- że SPAR uruchamia kod i shell;
- że nie jest pełnym sandboxem;
- gdzie zapisuje dane;
- jak wyczyścić run;
- jak bezpiecznie zgłosić błąd.

## 9.6. Instalacja i pakiet

Dopiero blisko publicznego alpha:

- instalacja przez `pipx` lub `uv tool`;
- wheel i sdist;
- `spar --version`;
- metadata projektu;
- README/licencja w paczce;
- test instalacji z gotowego wheela;
- osobny optional extra dla GUI;
- publikacja na PyPI.

---

# 10. CI i GitHub Actions

Brak pełnego GitHub Actions **nie jest obecnie problemem blokującym**.

Lokalne testowanie jest wystarczające, jeżeli:

- każda istotna poprawka ma test regresyjny;
- pełna suite jest regularnie uruchamiana;
- wynik jest zapisywany w HANDOFF;
- realne runy są powtarzane po zmianach w rdzeniu.

CI staje się potrzebne w momencie:

- przyjmowania zewnętrznych pull requestów;
- publikowania release’ów;
- wspierania kilku wersji Pythona;
- deklarowania kompatybilności systemowej;
- automatycznej publikacji pakietu.

Minimalny przyszły workflow może być tani:

- jedna wspierana wersja Pythona na każdy PR;
- pełna macierz tylko przy tagu/release lub ręcznym uruchomieniu;
- GUI tests opcjonalnie/osobno;
- real-CLI contract tests wyłącznie manualnie;
- cache zależności;
- concurrency cancellation dla starych commitów.

Nie należy teraz przesuwać pracy z recovery na CI tylko po to, aby repo wyglądało bardziej „profesjonalnie”.

---

# 11. Strategia open source i kontrybucji

SPAR dobrze nadaje się do open source, ponieważ różni użytkownicy mogą dostarczać:

- nowe warianty eventów CLI;
- fixture’y z realnych awarii;
- adaptery;
- poprawki platformowe;
- diagnostykę;
- ulepszenia dokumentacji;
- testy różnych build systemów;
- GUI polish.

## 11.1. Granica dla kontrybutorów

### Rdzeń wymagający szczególnie ostrożnej recenzji

- state machine;
- recovery;
- Git/worktree lifecycle;
- merge;
- guardy;
- wykonywanie shell commands;
- review acceptance;
- final merge;
- format state.

### Bezpieczniejsze obszary pierwszych kontrybucji

- fixture parserów;
- komunikaty błędów;
- dokumentacja;
- `doctor`;
- debug bundle;
- display-only streaming;
- GUI rendering;
- dodatkowe testy;
- platform detection;
- niewielkie adaptery po ustabilizowaniu interfejsu.

## 11.2. Pliki potrzebne przed zapraszaniem społeczności

- `CONTRIBUTING.md`;
- `CODE_OF_CONDUCT.md`;
- issue templates;
- PR template;
- opis uruchamiania testów;
- krótki dokument architektury;
- oznaczenia `good first issue`, `help wanted`, `adapter`, `recovery`, `docs`;
- polityka zgłaszania problemów bezpieczeństwa;
- informacja, które API nie jest stabilne.

## 11.3. Nie stabilizować plugin API za wcześnie

Publiczny interfejs adapterów i pluginów będzie atrakcyjny, lecz zbyt wczesna stabilizacja może zablokować refaktory.

Rekomendacja:

- na początku oficjalnie wspierać Claude + Codex;
- inne adaptery traktować eksperymentalnie;
- najpierw zebrać kilka implementacji;
- dopiero potem wyznaczyć minimalny stabilny kontrakt.

---

# 12. Funkcje, które mogą poczekać

Przed utwardzeniem rdzenia nie są priorytetem:

- wielu nowych providerów;
- pełny plugin marketplace;
- Windows native;
- automatyczny Docker sandbox;
- 2-way concurrency;
- rozbudowane statystyki kosztów;
- zaawansowana telemetria;
- współpraca wielu użytkowników;
- zdalne sterowanie;
- bardzo rozbudowany embedded terminal;
- rozbudowane theming GUI;
- stabilne publiczne Python API.

Token counters są przydatne i względnie małe, ale nie powinny wyprzedzać recovery.

Concurrency jest ciekawa, lecz znacząco zwiększa trudność:

- blokad;
- zależności;
- merge ordering;
- logów;
- kosztów;
- recovery;
- deterministyczności.

Sequential-first jest obecnie właściwą decyzją.

---

# 13. Proponowana ścieżka do wydania

## Faza A — hardening wewnętrzny

Cel: SPAR działa przewidywalnie pod nadzorem autora.

- kolejne realne runy;
- fault injection;
- domknięcie znanych recovery gaps;
- testy regresyjne;
- jawne inwarianty;
- cleanup runów;
- audit fingerprintów review/testów;
- decyzja dotycząca granicy GUI.

## Faza B — private/public alpha dla odważnych użytkowników

Cel: użytkownik techniczny potrafi zainstalować i odzyskać proces bez znajomości kodu SPAR-a.

- `doctor`;
- debug bundle;
- security model;
- known limitations;
- wersjonowanie stanu;
- pakiet instalacyjny;
- kilka przykładów;
- przynajmniej minimalne CI;
- jednoznaczny status „experimental alpha”.

## Faza C — beta open source

Cel: można przyjmować błędy i kontrybucje bez ręcznego odtwarzania każdego środowiska.

- stabilniejszy config;
- migracje stanu;
- contributor docs;
- release automation;
- test matrix;
- udokumentowany support systemów;
- wybrane extension points;
- regularne changelogi.

## Faza D — 1.0

`1.0` nie oznacza braku błędów. Powinno oznaczać:

- stabilny główny workflow;
- jasny compatibility contract;
- przewidywalne recovery;
- brak regularnej utraty lub nadpisywania pracy;
- stabilny format config;
- migracje albo jawna polityka stanu;
- określony security model;
- udokumentowane ograniczenia;
- zaufany release process.

---

# 14. Sugerowana macierz realnych testów

## Typy repozytoriów

- mały Python CLI;
- TypeScript/Node;
- C/C++ z Make/CMake;
- projekt z istniejącą rozbudowaną suite;
- monorepo;
- repo z generated files;
- repo z dużą liczbą untracked/ignored artifacts;
- repo z pre-commit hooks;
- repo z submodule;
- repo z nazwami plików zawierającymi spacje/unicode.

## Typy zadań

- jeden plik;
- kilka niezależnych plików;
- zadania z zależnościami;
- dwa taski dotykające tego samego pliku;
- refaktor bez zmiany zachowania;
- feature wymagający migracji;
- poprawka błędu;
- zadanie z niejednoznacznymi wymaganiami;
- zadanie, w którym reviewer słusznie odrzuca plan;
- zadanie, w którym test planu jest niemożliwy na izolowanym branchu.

## Zakłócenia

- Ctrl+C w każdej fazie;
- kill procesu modelu;
- timeout;
- brak binarki CLI;
- utrata sesji;
- invalid verdict;
- brak zmian implementera;
- zmiany poza scope;
- self-commit implementera;
- fail test;
- hang test;
- merge conflict;
- przesunięty target;
- usunięty worktree;
- ręczna zmiana pliku;
- brak miejsca na dysku;
- read-only filesystem;
- drugi proces SPAR;
- GUI i host-agent równocześnie.

---

# 15. Konkretne obserwacje z aktualnego repozytorium

1. **README jest obecnie mocne.** Szybko pokazuje różnicę między debatą, execution, testem i bramkami.

2. **`HANDOFF.md` jest wyjątkowo wartościowy dla developmentu z agentem.** Rejestruje realne awarie i przyczyny decyzji. Należy go zachować, ale z czasem oddzielić od publicznego changeloga.

3. **Test suite ma dobrą szerokość.** Obecność E2E i real-CLI contract tests jest ważniejsza na tym etapie niż badge CI.

4. **`orchestrator.py` i `exec/loop.py` są już duże.** Nie wymagają natychmiastowego refaktoru, ale dalsze funkcje powinny być dokładane z kontrolą odpowiedzialności.

5. **GUI zostało zbudowane bardzo szybko i ma sens jako pilot/dashboard.** Należy uważać, aby nie zmieniło projektu w pełne IDE agentów przed utwardzeniem engine’u.

6. **Review dispute gate to dobry przykład dojrzałej ewolucji protokołu.** System nie powinien arbitralnie zabijać procesu, gdy strony mają uzasadniony spór.

7. **`scope_ignore` jest pragmatyczną odpowiedzią na artefakty builda.** Ważne, aby nie przekształcić ignore w łatwą drogę obejścia zakresu. Patterns powinny być jawne w statusie/debugu.

8. **Model floors dla implementacji, debaty i review są sensowne.** Konfiguracja nie powinna jednak zbyt mocno wiązać się z nazwami chwilowo dostępnych modeli. Błędy nieznanego modelu muszą być czytelne.

9. **Live observability jest dużym atutem.** Użytkownik może zauważyć pętlę lub błędne działanie zanim zakończy się cały run.

10. **Brak publicznego release’u jest zgodny ze stanem projektu.** Repo nie powinno być jeszcze promowane jako bezpieczne narzędzie do dowolnego produkcyjnego kodu.

---

# 16. Najkrótsza rekomendowana kolejność prac

1. Kontynuować prawdziwe runy na różnych repozytoriach.
2. Zamieniać każdą awarię w fixture i test regresyjny.
3. Domknąć znane luki resume/recovery z `HANDOFF.md`.
4. Zrobić audyt inwariantów state ↔ Git.
5. Zrobić audyt ważności review i testów względem commitów/hashów.
6. Formalnie zdecydować, czy grill/terminal należą do rdzenia produktu.
7. Uporządkować ADR-y po tej decyzji.
8. Dodać security model.
9. Dodać `doctor` i bezpieczny debug bundle.
10. Dopiero potem przygotowywać pakiet, minimalne CI i publiczne alpha.
11. Po publicznym alpha otworzyć dobrze ograniczone obszary kontrybucji.
12. Stabilne plugin API i concurrency zostawić na później.

---

# 17. Ostateczny werdykt

SPAR nie wygląda jak przypadkowy eksperyment z dwoma modelami. Wygląda jak początek **poważnego silnika do kontrolowanego, wielomodelowego developmentu**.

Najważniejsze elementy już istnieją:

- wyraźna teza;
- niezależne strony;
- strukturalny protokół;
- konsensus;
- task planning;
- izolacja Git;
- cross-review;
- test gates;
- stan i resume;
- headless mode;
- obserwowalność;
- GUI-pilot;
- szerokie testy;
- rozwój na podstawie realnych runów.

Nie ma potrzeby zmieniać fundamentu. Projekt potrzebuje teraz przede wszystkim:

- hartowania poza happy pathem;
- formalizacji inwariantów;
- bezpiecznego recovery;
- doprecyzowania granicy produktu;
- jawnego modelu zaufania;
- przygotowania diagnostyki dla przyszłych użytkowników.

Największym zagrożeniem nie jest brak funkcji. Jest nim **zbyt szybkie rozszerzenie zakresu**, zanim rdzeń stanie się nudny, przewidywalny i odporny na przerwania.

Jeżeli obecny sposób pracy zostanie utrzymany — realny run, wykryta awaria, fixture, test, poprawka — SPAR ma realną szansę stać się wartościowym projektem open source oraz technicznie wyróżniającym się narzędziem w obszarze agentic software engineering.
