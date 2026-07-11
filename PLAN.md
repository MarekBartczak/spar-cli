> **Dokument historyczny** (v2, sprzed pivotu ADR 0003). Aktualne decyzje
> architektoniczne żyją w `docs/adr/`; aktualny opis narzędzia w `README.md`.
> Zachowany jako zapis pierwotnego projektu protokołu debaty/egzekucji.

# spar — plan projektu (v2, po rundzie recenzji Codex)

Narzędzie CLI orkiestrujące debatę dwóch agentów AI (Claude Code + Codex CLI) nad wspólnym artefaktem (plan projektu, spec, architektura, review). Agenci naprzemiennie edytują i krytykują artefakt aż do konsensusu; ostatecznym arbitrem jest użytkownik.

## 1. Cel i użycie

```bash
# w katalogu repo, którego dotyczy zadanie
spar "Zaplanuj migrację auth na OAuth2" \
  --sides claude,codex --first claude --max-rounds 6

spar --continue     # wznowienie przerwanej debaty
```

Przebieg:
1. Strona pierwsza tworzy wstępną wersję artefaktu (ma dostęp do repo: kod, CLAUDE.md/AGENTS.md, skille).
2. Strona druga czyta artefakt, edytuje go, odpowiada na uwagi, zgłasza własne, kończy werdyktem.
3. Tury naprzemienne aż do konsensusu (definicja: sekcja 3).
4. Bramka użytkownika: akceptacja = koniec; uwagi użytkownika wracają do pętli jako nowe zastrzeżenia.
5. Wynik: `.spar/artifact.md` + otwarte uwagi `[NICE]` jako backlog + pełna historia debaty.

Decyzje ustalone:
- Zakres generyczny (dowolny artefakt tekstowy), plan projektu = pierwszy use case.
- Pętla automatyczna z limitem rund; użytkownik może przerwać (Ctrl+C → stan zapisany).
- Sesje CLI wznawiane (`--resume` / `exec resume`) — **wyłącznie jako optymalizacja tokenów**. Źródłem prawdy jest stan orkiestratora: artefakt + strukturalna lista uwag (sekcja 4). Każda tura musi być wykonywalna od zera w świeżej sesji z samego stanu.
- Obie strony symetryczne: każda w swojej turze edytuje artefakt i krytykuje.
- Bez TUI; log tur na stdout, artefakt w pliku.
- Bez wpinania się w wewnętrzne API narzędzi — wyłącznie subprocess + strumienie JSON/JSONL.

## 2. Architektura

Nazewnictwo: pakiet/dystrybucja **`spar-cli`** (nazwa `spar` na PyPI zajęta), binarka i komenda **`spar`**. Rodzina nazw na przyszłość: `spar-desktop` (GUI) itd. — poza zakresem.

Python ≥3.11, stdlib `argparse` (bez typera), trzy warstwy:

```
spar/
  cli.py            # argparse: run / --continue
  orchestrator.py   # pętla debaty, konsensus, bramka usera, prompty tur
  guard.py          # kontrola kontraktu artefaktu po turze (sekcja 3a)
  adapters/
    base.py         # interfejs adaptera (poniżej)
    claude.py
    codex.py
  verdict.py        # parser bloku <verdict>
  state.py          # session.json: zapis atomowy, recovery
  config.py         # TOML: globalny + projektowy
```

Interfejs adaptera (świadomy strumieni, nie "jeden JSON"):

```python
class TurnResult:
    session_id: str | None   # None = nie udało się ustalić
    reply_text: str          # finalna wiadomość agenta
    events_path: Path        # surowy zrzut stream/JSONL do transkryptu
    exit_code: int

class Adapter(Protocol):
    def run_turn(self, prompt: str, session_id: str | None,
                 timeout_sec: int) -> TurnResult: ...
    # session_id=None → nowa sesja; str → próba resume;
    # fail resume → adapter zgłasza SessionLost, orkiestrator odpala nową sesję
```

**ClaudeAdapter** (do weryfikacji testem kontraktowym w zadaniu #4 — flagi CLI zmieniają się między wersjami):
- start: `claude -p --output-format json <prompt>`; `session_id` z pola `session_id` w wynikowym JSON; opcjonalnie wymuszenie własnego UUID przez `--session-id`.
- resume: `claude -p --resume <id> --output-format json <prompt>`.
- uprawnienia do edycji artefaktu: dokładna forma (`--allowedTools "Edit Write Read"` vs `--tools` vs `--permission-mode acceptEdits`) do ustalenia w teście kontraktowym; plan nie przesądza składni.

**CodexAdapter** (jw., test kontraktowy w zadaniu #5):
- `--json` to **strumień JSONL** (eventy), nie pojedynczy dokument: adapter czyta linia po linii, wyciąga `session_id` z eventów, toleruje malformed lines.
- finalna odpowiedź: `--output-last-message <plik>` (prostsze i pewniejsze niż składanie z eventów); JSONL zostaje jako metadane/transkrypt.
- kolejność argumentów: opcje globalne **przed** subkomendą: `codex exec --json --sandbox workspace-write --cd <repo> --output-last-message <f> <prompt>` oraz `codex exec --json ... resume <id> <prompt>`.
- sandbox `workspace-write` + `--cd <repo>`: `.spar/` musi leżeć pod writable rootem repo; jeśli artefakt poza repo — `--add-dir`.

**Config** — `~/.config/spar/config.toml` + nadpisania `.spar/config.toml`:

```toml
[sides.claude]
adapter = "claude"
command = "claude"          # dowolna binarka zgodna z interfejsem claude CLI
model  = ""                 # opcjonalnie wymuszenie modelu

[sides.codex]
adapter = "codex"
command = "codex"

[debate]
max_rounds = 6
turn_timeout_sec = 900
```

CLI agentów uruchamiane z cwd = repo docelowe. Interfejs adaptera projektowany pod dokładnie dwa backendy — bez generalizacji pod przyszłe CLI (YAGNI).

## 3. Protokół tury, werdykt, konsensus

Prompt tury zawiera:
- ścieżkę artefaktu (`.spar/artifact.md`) — agent czyta i **edytuje plik sam**,
- hash wersji artefaktu, którą agent ocenia,
- strukturalną listę otwartych uwag (przeciwnika + użytkownika) ze stanu orkiestratora — nie z pamięci sesji,
- instrukcję: odnieś się do każdej uwagi (przyjmij i popraw / odrzuć z uzasadnieniem), zgłoś własne zastrzeżenia, zakończ werdyktem.

Blok werdyktu (uwagi mają ID nadawane przez orkiestratora; agent musi rozstrzygnąć każdą otwartą):

```
<verdict>
status: AGREE | CONTINUE
resolved:
- #7 accepted
- #9 rejected: big-bang świadomie, feature flag podnosi złożoność
remarks:
- [MUST] Brak strategii rollback w kroku 3
- [NICE] Rozważ feature flag zamiast big-bang
</verdict>
```

Zasady:
- Każda uwaga z `pending_remarks` musi pojawić się w `resolved:` jako `accepted` albo `rejected: <uzasadnienie>`; brakująca = nadal otwarta. Orkiestrator aktualizuje `pending`/`resolved` wyłącznie z tego bloku — proza się nie liczy.
- `AGREE` nieważne, dopóki jakakolwiek uwaga `[MUST]`/`[USER]` pozostaje otwarta, oraz gdy werdykt zgłasza nowe `[MUST]`.
- **Explicite: `[NICE]` nie blokuje konsensusu** — otwarte `[NICE]` trafiają do backlogu w wyniku końcowym.
- Orkiestrator parsuje wyłącznie blok `<verdict>`; reszta odpowiedzi idzie do logu.
- **Konsensus = obie strony dały `AGREE` dla tego samego hasha artefaktu.** Werdykt jest przypisany do hasha wersji, którą agent widział na koniec swojej tury. Jeśli agent w turze zmienił artefakt, jego `AGREE` dotyczy nowej wersji — przeciwnik musi ją jeszcze potwierdzić. Praktycznie: debatę kończy tura bez modyfikacji artefaktu i z `AGREE`, następująca po turze z `AGREE` drugiej strony.
- Uwagi użytkownika z bramki wstrzykiwane jako `[USER]` — traktowane jak `[MUST]`, kasują stan konsensusu.

### 3a. Kontrola kontraktu po turze (guard)

Po każdej turze orkiestrator sprawdza:
- artefakt istnieje, jest tekstowy, niepusty;
- artefakt nie został wypatroszony (spadek objętości > 60% bez uwagi `[USER]` nakazującej cięcie → tura odrzucona, retry z ostrzeżeniem);
- agent może modyfikować **wyłącznie `.spar/artifact.md`** (`session.json` i `transcript/` pisze tylko orkiestrator); detekcja: snapshot listy plików + mtime/hash przed turą, w repo git — `git status --porcelain`; naruszenie → tura odrzucona **z rollbackiem skutków ubocznych do stanu sprzed tury** (nie do HEAD): nowe pliki utworzone w turze — usuwane; pliki, które przed turą były czyste względem HEAD (wg pre-turn `git status`) — `git checkout -- <plik>`; pliki brudne już przed turą — **brak bezpiecznego rollbacku → abort z dokładną listą** (orkiestrator nie trzyma kopii treści całego repo, więc nie zgaduje); poza repo git analogicznie: auto-rollback tylko usuwanie nowych plików, modyfikacje = abort z listą; po udanym rollbacku retry z ostrzeżeniem, drugi fail = abort;
- odpowiedź kończy się parsowalnym werdyktem.

## 4. Stan i pliki

```
.spar/
  config.toml          # opcjonalne nadpisania projektowe
  artifact.md          # artefakt debaty (jedyne źródło prawdy o treści)
  session.json         # stan debaty (jedyne źródło prawdy o przebiegu)
  transcript/
    round-01-claude.md     # finalna odpowiedź agenta
    round-01-claude.jsonl  # surowe eventy
    ...
```

`session.json` — stan strukturalny, nie skrót transkryptu:

```json
{
  "round": 3,
  "last_actor": "codex",
  "turn_in_progress": null,
  "artifact_hash": "sha256:…",
  "sides": {
    "claude": {"session_id": "…", "last_verdict": {"status": "AGREE", "artifact_hash": "…"}},
    "codex":  {"session_id": "…", "last_verdict": null}
  },
  "pending_remarks":  [{"id": 7, "severity": "MUST", "author": "codex", "text": "…"}],
  "resolved_remarks": [{"id": 3, "resolution": "accepted", "…": "…"}]
}
```

- Zapis atomowy (temp + rename) po każdej zakończonej turze.
- **Blokada pojedynczej instancji**: `fcntl.flock(LOCK_EX | LOCK_NB)` na `.spar/lock`, trzymany przez cały czas życia procesu; nieuzyskany lock → odmowa startu z komunikatem. Kernel zwalnia lock przy śmierci procesu (także SIGKILL) — brak stale locków, brak protokołu przejęcia, brak race'a. Pid + start zapisywane do pliku tylko informacyjnie (komunikat "kto trzyma").
- **Recovery po przerwaniu w trakcie tury**: przed startem tury zapisywane `turn_in_progress = {side, artifact_hash_before}`. `--continue` z niepustym `turn_in_progress`: porównaj aktualny hash artefaktu z `artifact_hash_before` — zgodny → powtórz turę; różny → pokaż userowi diff i wybór (przyjmij zmiany i powtórz turę / przywróć hash sprzed tury z transkryptu). Snapshot artefaktu sprzed tury trzymany w `transcript/`.
- Utrata sesji CLI (resume fail) → nowa sesja; prompt i tak zawiera pełny potrzebny kontekst (artefakt + `pending_remarks`), bo pamięć sesji nie jest źródłem prawdy.

## 5. Obsługa błędów

| Sytuacja | Reakcja |
|---|---|
| Brak / nieparsowalny blok `<verdict>` | 1 retry w tej samej sesji: prompt żąda **wyłącznie poprawnego bloku verdict, zakaz edycji artefaktu** (guard: hash artefaktu nie może się zmienić podczas retry); drugi fail = abort z zapisanym stanem |
| CLI kończy się błędem (exit ≠ 0) | 1 retry; potem abort z komunikatem i stanem do `--continue` |
| Timeout tury | kill procesu, potem recovery jak przy przerwaniu w trakcie tury (sekcja 4) |
| Utrata sesji (resume nie działa) | nowa sesja; kontekst odtwarzany ze stanu (artefakt + pending_remarks) |
| Naruszenie kontraktu artefaktu (sekcja 3a) | tura odrzucona, artefakt przywrócony ze snapshotu, **skutki uboczne poza artefaktem wycofane (rollback z sekcji 3a)**, 1 retry z ostrzeżeniem; drugi fail = abort |
| Druga instancja spar na tym samym `.spar/` | odmowa startu (`flock` niedostępny); po śmierci właściciela kernel zwalnia lock sam |
| Limit rund osiągnięty bez konsensusu | wymuszona bramka użytkownika: artefakt + pending_remarks, user decyduje (akceptuj / dogrywka +N rund / przerwij) |
| Ctrl+C | zapis stanu (turn_in_progress), czysty exit, `--continue` z procedurą recovery |

## 6. Testy

- **Unit**: parser werdyktów (brak bloku, zły status, mieszane wagi, blok w środku tekstu), logika konsensusu per-hash, guard artefaktu, serializacja stanu + recovery, merge configów.
- **Adaptery — testy kontraktowe na fejkowych binarkach** odwzorowujących realne zachowania: JSONL Codexa (w tym malformed lines, brak session_id, partial output + exit ≠ 0), single-result JSON Claude, timeout, finalna wiadomość bez werdyktu, `--output-last-message`. Fejki generowane z nagranych outputów prawdziwych CLI (fixture'y).
- **Kontrakt z prawdziwymi CLI (opt-in, flaga env)**: weryfikacja składni flag (`--resume`, `--session-id`, `--allowedTools`/`--tools`, kolejność argumentów `codex exec … resume`) + pełna debata 2 rundy na trywialnym artefakcie. Uruchamiane ręcznie i przy bumpach wersji CLI.

## 7. Zadania implementacyjne (z przypisaniem modeli)

Przypisania modeli zgodnie z konwencją użytkownika (globalny CLAUDE.md).

| # | Zadanie | Model |
|---|---------|-------|
| 1 | Szkielet pakietu, pyproject, CLI (argparse), struktura katalogów | Haiku 4.5 |
| 2 | `config.py` — TOML, merge globalny/projektowy, walidacja | Haiku 4.5 |
| 3 | `verdict.py` — parser + testy unit | Sonnet 4.6 |
| 4 | `adapters/base.py` + `claude.py` + test kontraktowy flag (start/resume/session_id/uprawnienia) | Sonnet 4.6 |
| 5 | `adapters/codex.py` — JSONL stream, `--output-last-message`, resume, sandbox/cd + test kontraktowy | Sonnet 4.6 |
| 6 | `state.py` — session.json, zapis atomowy, `turn_in_progress`, recovery | Sonnet 4.6 |
| 7 | `orchestrator.py` — pętla debaty, konsensus per-hash, bramka usera, prompty tur | Opus 4.7 |
| 8 | `guard.py` + obsługa błędów i retry (sekcja 5) | Sonnet 4.6 |
| 9 | Fejkowe binarki z fixture'ów + testy adapterów i pętli | Sonnet 4.6 |
| 10 | Testy kontraktowe opt-in z prawdziwymi CLI + README | Haiku 4.5 |

## Poza zakresem v1

- **Tryb egzekucji (kierunek v2, celowo nieprojektowany tutaj)**: po konsensusie nad planem — podział tasków między strony, praca równoległa w osobnych git worktree/branch per strona, cross-review zaimplementowanych tasków przez drugą stronę, maszyna stanów tasków, merge. Buduje się na tych samych adapterach i protokole werdyktów; wymaga własnego planu.
- **GUI (`spar-desktop`)**: przyszła granica = pliki stanu `.spar/` (już czytelne dla zewnętrznych procesów) + ewentualne `spar serve` (lokalny HTTP/WS nad orkiestratorem). Silnik projektowany tak, by ta warstwa była cienka; nic więcej teraz.
- TUI, tryb równoległych propozycji, więcej niż 2 strony, integracja przez Agent SDK.

## Historia recenzji

- **Runda 1 (Codex, CONTINUE)**: przyjęte — składnia resume/session_id Claude (do testu kontraktowego), JSONL Codexa + `--output-last-message`, kolejność argumentów `codex exec resume`, sandbox `--cd`/writable root, stan strukturalny zamiast pamięci sesji, guard artefaktu, konsensus per-hash, retry werdyktu bez edycji, `turn_in_progress` + recovery, realistyczne fixture'y, argparse, usunięcie aliasów z przykładu configu, YAGNI na przyszłe CLI. Odrzucone — usunięcie przypisań modeli z zadań (konwencja użytkownika wymaga modelu per zadanie).
- **Runda 2 (Codex, CONTINUE)**: przyjęte — werdykt z ID uwag i sekcją `resolved:` (mechaniczne domykanie uwag zamiast prozy), doprecyzowanie guarda (agent pisze tylko `artifact.md`). Odrzucenie ws. modeli zaakceptowane przez recenzenta.
- **Runda 3 (Codex, CONTINUE)**: przyjęte — #1 [MUST] rollback skutków ubocznych poza artefaktem po naruszeniu guarda (git checkout / usunięcie nowych plików / abort z listą poza gitem), #2 [NICE] lockfile pojedynczej instancji. Odrzuceń brak.
- **Runda 4 (Codex, CONTINUE)**: przyjęte — #3 [MUST] rollback do stanu sprzed tury, nie do HEAD (pliki brudne przed turą = abort z listą zamiast zgadywania), #4 [MUST] atomowe utworzenie locka (`O_EXCL`, przejęcie przez rename). Odrzuceń brak. Limit rund osiągnięty → arbitraż użytkownika (user przyznał dogrywkę).
- **Runda 5 (Codex, CONTINUE, dogrywka)**: #3 potwierdzone naprawione; #5 [MUST] przejęcie martwego locka przez rename nadal race'owalne → przyjęte, mechanizm zastąpiony `fcntl.flock` (kernel zwalnia przy śmierci procesu, protokół przejęcia zbędny).
