# ai-mobile-e2e

Flutter アプリの E2E テストを、**各工程を AI エージェントに置き換えたハーネス**として
実行するツールキット。

```
設計書(人 / Excel・Confluence)
  → 取り込み(e2e ingest) → 試験項目生成(AI) → テストコード生成(AI)
  → 自動実行(Appium) → 証跡取得(自動) → 結果確認(人)
```

このツールキットは**アプリのリポジトリとは別のリポジトリ**として管理し、開発端末上で
アプリフォルダの配下に置いて使う。アプリのソースを取り込む必要はない。

## 設計の考え方

**進行管理と検証は決定論的なコードが持ち、AI には各工程の生成だけを任せる。**

GitHub Copilot には複数エージェントを制御する API が無い。またワークフローの進捗・
再開・検証ゲートを AI の判断に委ねると、静かに工程を飛ばしたり検証失敗を握りつぶし
たりする。そのため:

- **オーケストレータ**: Python 製の `e2e` コマンド。工程の順序、状態の永続化、
  検証ゲート、リトライ、人のレビュー地点での停止を担う。
- **各工程のエージェント**: `prompts/` にプロンプト本体を単一ソースで置き、
  `.github/agents/*.agent.md`(Copilot のカスタムエージェント)を生成する。

同じプロンプト資産が 2 つの実行経路から使われる。

| 経路 | 使い方 |
|---|---|
| 自動 | `e2e run` が `copilot -p ... --agent=...` を各工程で呼ぶ |
| 対話 | VS Code のチャットで `e2e-orchestrator` を選び、`handoffs` で工程を辿る |

Copilot CLI が入っていない端末では自動的に**手動モード**になり、工程ごとのプロンプトを
ファイルに書き出す。人がそれをチャットに貼って実行し、`e2e resume` で先へ進める。

## ハルシネーション対策

テストコードは Python で書くため、TypeScript の `tsc` に相当する層が無い。
代わりに 3 層で AI の嘘を弾く。

```
アプリのソース(別リポジトリ)
  │ e2e scan-app … Semantics(identifier:) を静的走査
  ▼
locators/registry.yaml   ← ロケータの単一ソース
  │ e2e gen-pages   … Page Object を自動生成(手書き禁止)
  ▼
tests/pages/*.py         ← AI はここのメソッドしか呼べない
  │ e2e gate codegen … 生成テストを AST 走査し未定義参照を落とす
  ▼
tests/e2e/*.py           ← AI 生成テスト
```

1. **走査との突合** — レジストリにあってアプリに無い identifier を検出する。
   レジストリ自体の嘘を捕まえる層。
2. **Page Object の生成** — AI にロケータ文字列を書かせない。
3. **AST 検証** — 存在しない Page Object やメソッドの呼び出しを行番号付きで落とす。
   `ruff` と `pytest --collect-only` も同じゲートで走る。

**証跡取得も AI に書かせない。** `tests/conftest.py` の pytest フックが自動で取得する。

## 配置と初期設定

このツールキットは**アプリフォルダの配下に置くだけ**で動く。アプリのソースを
取り込む必要はなく、`e2e.config.yaml` の `app.root` から相対的に参照する。

配布のしかたは 2 通りある。どちらでも手順はほぼ同じで、違いは
「Copilot にエージェントの場所をどう教えるか」だけ。

| | A. zip で配る | B. アプリのリポジトリに同梱する |
|---|---|---|
| 置き方 | zip をアプリフォルダ配下に展開する | アプリリポジトリに含めて push する |
| エージェント定義 | ハーネス配下に置き、設定で場所を教える | アプリ直下の `.github/agents/` に出力する |
| 追加設定 | `chat.agentFilesLocations` が必要 | **不要** |

### 1. アプリフォルダ配下に置く

```
your-flutter-app/
├── lib/
├── build/
└── tools/
    └── ai-mobile-e2e/     ← ここに置く
```

zip で配る場合は展開するだけ。配布用の zip は `e2e package` で作れる。

```bash
uv run e2e package --output ai-mobile-e2e.zip
```

`.git`・`node_modules`・`.venv`・`artifacts` は含まれない。展開すると
`ai-mobile-e2e/` の 1 階層になる。

### 2. 依存を入れる

```bash
cd tools/ai-mobile-e2e
uv sync                     # Python 3.12 + Appium-Python-Client
npm install                 # Appium 本体
npx appium driver install uiautomator2
npx appium driver install xcuitest
```

いずれもネットワークが要る。配布先の端末で外部通信が制限されている場合は、
`e2e package --include-node-modules` で Appium 本体ごと固めて配る。

### 3. 対象アプリ向けに初期化する

```bash
uv run e2e init --app-root "../.." --package com.example.yourapp
```

`e2e.config.yaml` が対象アプリ向けに作り直され、エージェント定義が生成される。
`e2e.config.yaml` を直接書き換えても同じ。

### 4. Copilot にエージェントの場所を教える

**B(アプリリポジトリに同梱)の場合** — アプリ直下の `.github/agents/` に出力する。
VS Code が既定で探索する場所なので、追加の設定はいらない。

```bash
uv run e2e sync-agents --output ../../.github/agents
```

**A(zip 配布)の場合** — エージェント定義はハーネス配下に残るため、VS Code に
場所を教える必要がある。これが無いと Copilot はエージェントを見つけられない。
アプリ側の `.vscode/settings.json` に追加する:

```json
{
  "chat.agentFilesLocations": {
    "tools/ai-mobile-e2e/.github/agents": true
  }
}
```

`e2e init` がこの JSON をパス付きで出力するので、そのまま貼れる。
ハーネスのフォルダ自体を VS Code で開く運用なら、この設定は不要。

### 5. 前提条件を確認する

```bash
uv run e2e doctor
```

アプリのソース、ビルド成果物、Appium CLI、**Appium サーバが応答するか**、
adb、Copilot CLI、レジストリ、エージェント定義をまとめて確認する。

Appium サーバとエミュレータ/シミュレータの起動はハーネスの外にある。
別のターミナルで先に立ち上げておくこと。

```bash
npx appium --port 4723        # 別ターミナルで起動しておく
```

## 設計書の取り込み

設計書は Excel や Confluence にあることが多い。手で Markdown に起こす必要はなく、
`e2e ingest` が変換する。

```bash
uv run e2e ingest ~/Downloads/ログイン機能_設計書.xlsx
# → specs/ログイン機能_設計書.md
```

| 元の形式 | 用意するもの |
|---|---|
| Excel | `.xlsx` / `.xlsm` をそのまま |
| Confluence | ページを **HTML でエクスポート**したファイル |
| その他 | `.csv` / `.md` / `.txt` |

Excel は**結合セルの値を被覆範囲へ展開する**。日本語の設計書は画面名などを
結合セルで表すことが多く、展開しないと項目が画面に紐付かなくなるため。
全シートが対象で、`--sheet` で絞り込める。

```bash
uv run e2e ingest 設計書.xlsx --sheet 画面項目 --sheet 操作フロー
```

Confluence の HTML には本文だけでなく**パンくず・サイドバー・ラベル・
コメント欄・フッタ**が入ってくる。取り込み時に本文の領域
(`#main-content` / `.wiki-content` など)を特定して、それ以外は落とす。

これは体裁の問題ではない。コメント欄には「この画面のロック仕様どうなってますか?」
のような未確定の会話が並んでおり、これを仕様として取り込むと後続の工程が
存在しない仕様の試験項目を作ってしまう。

本文の領域を特定できなかった場合はページ全体を取り込んだうえで警告を出すので、
そのときは変換結果に目を通すこと。

**PDF は対応していない。** 表のレイアウトが失われて情報が壊れるため、元の Excel か
Confluence の HTML エクスポートを使うこと。

### 変換の品質は気にしなくてよい

変換結果は体裁が整っていない。元の表がそのまま写り、改訂履歴シートや
レイアウト用の空列も残る。**それで構わない。** 目的は人が読んで美しい Markdown を
作ることではなく、後続のエージェントが情報を落とさずに読めることにある。

雑な変換でも安全なのは、仕様の正規化工程に `open_questions` のゲートがあるため。
変換で情報が欠けていれば、spec-analyst が推測で埋めずに
「元の設計書に何を追記してほしいか」という形で差し戻し、ワークフローは止まる。
体裁を整える作業に人手をかける必要はない。

## 実行前の準備(毎回)

テストは手元で実行する。エミュレータと Appium サーバの起動、アプリの投入は
ハーネスの外にあるので、先に済ませておく。

```bash
# 1. エミュレータを起動する(別ターミナル)
emulator -avd <AVD名>

# 2. Appium サーバを起動する(別ターミナル)
npx appium --port 4723

# 3. アプリをビルドして投入する
cd <アプリのルート>
flutter build apk --debug --target-platform android-arm64 --split-per-abi
adb install -r build/app/outputs/flutter-apk/app-arm64-v8a-debug.apk

# 4. 準備できているか確認する
cd e2e && uv run e2e doctor
```

**手順 3 を飛ばさないこと。** Appium は署名とバージョンが同じ APK の再インストールを
スキップするため、アプリを直しても古いビルドがテストされ続ける。
「直したはずの不具合でテストが落ち続ける」「付けたはずの identifier が
実行時に見つからない」という症状が出たら、まずここを疑う。

`e2e doctor` はエミュレータの接続、Appium サーバの応答、ビルド成果物の有無を
まとめて確認するので、実行前に一度通しておくと原因切り分けが速い。

## 使い方

```bash
# 設計書を取り込む(Excel / Confluence から)
uv run e2e ingest ~/Downloads/ログイン機能_設計書.xlsx

# ワークフローを開始する
uv run e2e run --spec specs/ログイン機能_設計書.md --platform android

# 進捗を見る
uv run e2e status

# 人のレビュー地点で止まるので、確認して承認する
uv run e2e approve review
uv run e2e resume
```

### 工程

| # | 工程 | 担当 | 出力 |
|---|---|---|---|
| 0 | 設計書の取り込み (`e2e ingest`) | コマンド | `specs/<機能>.md` |
| 1 | `spec` 仕様の正規化 | `e2e-spec-analyst` | `spec.yaml` |
| 2 | `testcases` 試験項目の設計 | `e2e-testcase-designer` | `testcases.yaml` |
| 3 | `review` 試験項目のレビュー | **人** | 承認 |
| 4 | `locators` ロケータ整備 | `e2e-locator-curator` | `registry.yaml` |
| 5 | `codegen` テストコード生成 | `e2e-test-codegen` | `tests/e2e/test_*.py` |
| 6 | `execute` 実行と証跡取得 | コマンド | `evidence/` |
| 7 | `analyze` 結果の分析 | `e2e-run-analyst` | `analysis.yaml` |
| 8 | `confirm` 結果確認 | **人** | 完了 |

工程 3 に人のレビューを置いているのは、テストコードを読むより試験項目を読む方が
速く誤りに気付けるため。

### その他のコマンド

```bash
uv run e2e stages           # 工程の一覧
uv run e2e ingest <ファイル>  # Excel / Confluence の設計書を取り込む
uv run e2e scan-app         # アプリの Semantics(identifier:) を走査
uv run e2e gen-pages        # レジストリから Page Object を再生成
uv run e2e gate <工程>       # 特定工程の検証ゲートだけ実行
uv run e2e resume --run <ID> # 過去の実行を再開
uv run e2e sync-agents      # プロンプトからエージェント定義を再生成
uv run e2e package          # 配布用の zip を作る
```

プロンプト(`prompts/`)を直すたびに `e2e sync-agents` を実行する。
エージェント定義は生成物なので、直接編集しても次の同期で上書きされる。

## アプリ側に必要な対応

テストで触る要素には `Semantics` による identifier の付与が必要。
**`container: true` を必ず明示すること。**

```dart
Semantics(
  container: true,
  identifier: 'login_submit_button',
  child: ElevatedButton(...),
)
```

`container: true` が無いと兄弟ノードとマージされ、identifier がネイティブ側に
露出しなくなる。この挙動は実測で確認済みで、詳細と検証結果は
[`docs/phase0-findings.md`](docs/phase0-findings.md) にある。

`e2e scan-app` がこの書き漏れを検出し、`locators` 工程の検証ゲートが
ブロックするため、見落としたまま先に進むことはない。

## 動作確認の記録

このハーネスは、専用に用意した検証用 Flutter アプリ(ログイン画面・ホーム画面)に
対して全 8 工程を通し、以下を実測で確認したうえで作られている。検証用アプリ自体は
リポジトリには含めていない。

- **AST 検証** — 存在しない Page Object の import と存在しないメソッド呼び出しを、
  行番号付きで検出する
- **走査ゲート** — `container: true` の書き漏れを 9 件中 1 件だけ正確に摘出する
- **スキーマゲート** — `open_questions` の残存と不正な列挙値を位置情報付きで検出する
- **実行と証跡** — Android エミュレータで試験項目 7 件が通過。失敗時には
  `failure.png` / `page_source.xml` / `logcat.txt` / `recording.mp4` が
  試験項目 ID ごとのディレクトリに揃う
- **停止と再開** — 人のレビュー地点(工程 3・8)で停止し、`e2e approve` で再開する

なお **iOS はこのハーネス経由での実行が未検証**。identifier の露出と操作自体は
`docs/phase0-findings.md` のとおり iPhone シミュレータで確認済みで、Page Object にも
プラットフォーム差を吸収する実装が入っているが、通しで動かしてはいない。

## リポジトリ構成

```
├── e2e.config.yaml          # 端末ごとの設定はここだけ
├── prompts/                 # エージェントのプロンプト本体(単一ソース)
├── agents.yaml              # frontmatter のメタ情報
├── .github/agents/          # 生成物。Copilot が読む
├── schemas/                 # 工程間の契約(JSON Schema)
├── specs/                   # 人が書く設計書 (SPEC_TEMPLATE.md が雛形)
├── locators/registry.yaml   # ロケータの単一ソース
├── src/e2e_harness/         # オーケストレータ
├── tests/
│   ├── conftest.py          # 証跡の自動取得
│   ├── pages/               # base.py 以外は生成物
│   └── e2e/                 # AI 生成テスト
└── artifacts/<run-id>/      # 成果物・証跡・状態 (git 管理外)
```
