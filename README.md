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

## 前提条件のセットアップ

**identifier を付けただけではテストは動かない。** identifier は画面上の要素を
見つけるためのもので、**その画面に到達する手段は別に要る**。ログイン、規約同意、
ディープリンク起動などがこれにあたる。

試験項目の `preconditions` は ID を持ち、ID ごとに `tests/setup/setup_<id>` が
実装される。実装は AI が行い、**実装漏れは実行前のゲートで落ちる**。

```yaml
preconditions:
  - id: logged_in
    description: 一般ユーザーでログイン済みであること
    params:
      email: "user@example.com"
      password: "correct-password"
```

```
✗ テストコードの生成 の検証ゲートが落ちました:
  - 前提条件のセットアップが 2 件ありません。テストがその画面にたどり着けず、実行時に失敗します。
      - 'logged_in' (一般ユーザーでログイン済みであること): setup/ に setup_logged_in が必要です
```

セットアップは `tests/setup/` に蓄積され、複数の試験項目・複数の機能から
使い回される。同じ `logged_in` を機能ごとに作り直さないよう、
試験項目の工程で ID を揃えさせている。

### テストアカウント

会員種別が複数ある場合(カード会員 / カードレス会員など)、
**認証情報は試験項目にもテストコードにも書かない。** id で参照する。

```yaml
preconditions:
  - id: logged_in
    description: カード会員でログイン済みであること
    params:
      account: card_member
```

```python
setup_logged_in(driver, platform, **account("card_member"))
```

値は `testdata/accounts.yaml` の 1 か所にある。設計書に書かれたアカウントを
spec-analyst が抽出し、**既存の定義を上書きせずに蓄積する**(別の機能の
設計書が同じアカウントを違う値で書いていても壊さないため)。

```yaml
accounts:
  - id: card_member
    description: カードを保有している会員
    attributes:
      email: "card@example.com"
      password: "..."
  - id: cardless_member
    description: カードを保有していない会員
    attributes:
      email: "cardless@example.com"
      password: "..."
```

パスワードが変わったときに直す場所がここだけで済む。試験項目ごとに値を
埋め込む形にすると、変更のたびに全件を追うことになる。

**会員種別で挙動が変わる観点だけ**を種別ごとに作る。関係ない観点まで
機械的に 2 倍にすると、実行時間が倍になるわりに得られるものが少ない。

### ディープリンクと QR

ディープリンクは `BasePage.open_deeplink(url)` で開く。端末の機能なので
Page Object の生成対象ではなく、harness が持っている。

**QR コードの読み取りそのものは自動化しない。** QR が指す URL を
`open_deeplink()` に渡して代替する。カメラでの読み取りは Appium の守備範囲外で、
試験項目の意図(遷移先が正しいか)はこれで満たせる。

## 配置と初期設定

**アプリのリポジトリに含めて配る。** ハーネス一式をアプリリポジトリの一部として
管理し、チームは通常の `git pull` で受け取る。

この形にする理由は 2 つある。

- **`locators/registry.yaml` はアプリのソースと同期していないと壊れる。**
  registry の identifier はアプリの `Semantics(identifier:)` と完全一致していないと
  検証ゲートで落ちる。アプリ側で identifier をリネームしたら、registry も
  同じコミットで直す必要がある。別管理にすると構造的にこれができない。
- **試験項目とテストコードが合流しない。** 別管理だと各自のコピーが分岐し、
  数ヶ月かけて育つ `tests/e2e/` が共有されない。

### 1. アプリリポジトリ配下に置く

```
your-flutter-app/
├── lib/
├── build/
├── .github/agents/         ← エージェント定義をここに出力する
└── e2e/                    ← ハーネス一式
```

アプリのルートにある `.gitlab-ci.yml` や `.gitignore` には手を入れない。
`.gitignore` はネストが効くので、`e2e/.gitignore` がそのまま働く。

配置したら、アプリのルート `.gitignore` に巻き込まれていないか確認しておく。
何も出力されなければよい。

```bash
git check-ignore -v e2e/uv.lock e2e/package-lock.json e2e/e2e.config.yaml
```

### 2. 依存を入れる

```bash
cd e2e
uv sync                     # Python 3.12 + Appium-Python-Client
npm install                 # Appium 本体
npx appium driver install uiautomator2
npx appium driver install xcuitest
```

### 3. 対象アプリ向けに初期化する

```bash
uv run e2e init \
  --android-package com.example.yourapp \
  --ios-bundle-id com.example.yourapp
```

これで次の 2 つが行われる。

- `e2e.config.yaml` が対象アプリ向けに作り直される(直接書き換えても同じ)
- **エージェント定義が 2 か所に出力される**

### エージェント定義が 2 か所に要る理由

VS Code と Copilot CLI で探索の仕方が違うため、**両方に置かないと片方の経路で
エージェントが 1 つも見つからない。**

| 使うもの | 探す場所 |
|---|---|
| VS Code | ワークスペース直下の `.github/agents/` **のみ**。サブディレクトリは見ない |
| Copilot CLI | 作業ディレクトリから **git ルート**までの `.github/agents/` |

ハーネスを `git clone` で持ってきた場合、`e2e/` 自体が git ルートになる。
このとき Copilot CLI は `e2e/.github/agents/` を見るので、そこに無いと

```
No such agent: e2e-spec-analyst, available:
```

となって実行できない。一方 VS Code はアプリのルートを開いて使うので、
そちらにも要る。`e2e init` は両方へ出力する。

`prompts/` を直したときは同期を再実行する(こちらも両方に書く)。

```bash
uv run e2e sync-agents --output ../.github/agents
```

> GitLab のリポジトリなのに `.github/` ができるのは、これが VS Code の規約で
> ホスティング先と無関係なため。チームに先に説明しておくと余計な議論を避けられる。

### 4. 前提条件を確認する

```bash
uv run e2e doctor
```

アプリのソース、ビルド成果物、Appium CLI とサーバの応答、adb、実機の接続、
アプリの投入、iOS の署名設定、レジストリ、エージェント定義をまとめて確認する。
詰まったらまずこれを叩く。

**doctor は助言ツールで、進行をブロックしない。** `e2e run` は doctor の結果に
関係なく進む。結果は 3 段階で表示される。

| 表示 | 意味 |
|---|---|
| `✓` | 満たしている |
| `✗` | **必須**。これが欠けるとテストを実行できない |
| `△` | **任意**。欠けても運用できる(Copilot CLI など) |
| `-` | **対象外**。確認対象から外したプラットフォームの項目 |

終了コードは `✗`(必須)の数だけで決まる。**すべてを `✓` にする必要はない。**

片方のプラットフォームだけ試す日は絞ると、対象外の項目が判定から外れる。

```bash
uv run e2e doctor --platform android
```

実機の接続確認では、設定した `udid` が実際に見えている端末と一致するかまで見る。
一致しない場合は接続中の端末一覧を表示するので、そのまま設定に写せる。

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

雑な変換でも安全なのは、**読み取れなかった点が記録されて人に提示される**ため。
変換で情報が欠けていれば、spec-analyst が推測で埋めずに `open_questions` に挙げ、
それが工程 3(試験項目のレビュー)で提示される。体裁を整える作業に人手をかける
必要はない。

## 実機の準備(初回のみ)

テストは**実機**で実行する。端末ごとに一度だけ次の準備が要る。

### Android 実機

1. 端末の「開発者向けオプション」→「USB デバッグ」を有効にする
2. USB で接続し、端末に出る「USB デバッグを許可しますか」を許可する
3. シリアル番号を確認して設定に書く

```bash
adb devices -l
```

```yaml
appium:
  android:
    udid: "<adb devices で表示されたシリアル>"
```

**`udid` は必ず設定する。** `device_name` は端末の選択には使われないため、
複数台つないでいると意図しない端末でテストが走る。

### iOS 実機

こちらは手間がかかる。**Appium は WebDriverAgent というアプリを端末に
インストールして操作するため、実機では Apple の署名が必要になる。**

1. 端末を USB 接続し、「このコンピュータを信頼」を許可する
2. 端末の「設定 > プライバシーとセキュリティ > デベロッパモード」を有効にする
   (iOS 16 以降。有効化には端末の再起動が要る)
3. UDID と Apple Developer の Team ID を設定に書く

```bash
xcrun devicectl list devices        # UDID を確認
```

```yaml
appium:
  ios:
    udid: "<端末の UDID>"
    platform_version: "<端末の iOS バージョン>"
    xcode_org_id: "<Apple Developer の Team ID (10 桁)>"
    xcode_signing_id: "iPhone Developer"
```

**`xcode_org_id` が未設定・誤りだと `xcodebuild` が exit code 65 で落ちる。**
これが iOS 実機で最初に当たる壁で、エラーメッセージからは署名の問題だと
分かりにくい。`e2e doctor` が未設定を検出する。

無料の Apple ID を使う場合は Bundle ID の重複を避けるため
`updated_wda_bundle_id` の指定も要る。

> WebDriverAgent の署名を毎回やり直したくない場合は、事前にビルドして端末へ
> インストールしておく運用もできる。Appium の
> [Run Preinstalled WDA](https://appium.github.io/appium-xcuitest-driver/latest/guides/run-preinstalled-wda/)
> を参照。

## 実機での確認手順(最初に一度だけ)

**この方式が実機で成立するかは、まだ確認されていない。** Phase 0 の検証は
エミュレータとシミュレータで行っており、実機は未検証(下の「未検証の範囲」を参照)。

いきなり全画面に `Semantics` を付けて回ると、方式が成立しなかったときの手戻りが
大きい。**1 画面・数要素で下の順に確かめてから展開する。** 各段階で止まれば、
そこまでの作業しか無駄にならない。

### 0. 設定を埋める

```yaml
appium:
  android:
    udid: "<adb devices -l のシリアル>"
  ios:
    udid: "<xcrun devicectl list devices の UDID>"
    platform_version: "<端末の iOS バージョン>"
    xcode_org_id: "<Apple Developer の Team ID>"
```

### 1. 端末が見えているか

確認するプラットフォームを絞って実行する。

```bash
uv run e2e doctor --platform android
uv run e2e doctor --platform ios
```

**通過条件**: 「実機の接続」が ✓ になる。`△`(任意)は残っていてよい。

落ちる場合は USB 接続、Android の USB デバッグ許可、iOS の「このコンピュータを
信頼」とデベロッパモードを確認する。

### 2. アプリに identifier を 3〜5 個だけ付ける

テスト対象にする画面を 1 つ選び、代表的な要素にだけ付ける。
**この段階では画面すべてに付けない。**

```dart
Semantics(
  container: true,
  identifier: 'login_submit_button',
  child: ElevatedButton(...),
)
```

付けたらレジストリにも登録し、走査で拾えることを確認する。

```bash
uv run e2e scan-app
```

**通過条件**: 付けた identifier が一覧に出て、`!!`(container 漏れ)が付かない。

### 3. 端末で実際に操作できる状態にする

identifier を付けたアプリをビルドして端末に入れる。**IDE の実行ボタンでよい。**

- Android Studio の実行ボタン(debug で問題ない)
- Xcode の実行ボタン(**スキームを Release にしておくこと**)

Appium サーバを別ターミナルで起動する。

```bash
npx appium --port 4723
```

### 4. セッションが張れて、画面が取れるか

端末で対象の画面を開いた状態にして実行する。

```bash
uv run e2e inspect --platform android
uv run e2e inspect --platform ios
```

**通過条件**: スクリーンショットとアクセシビリティツリーが保存される。

**iOS 実機で最初に詰まるのはここ。** WebDriverAgent の署名が通らないと
`xcodebuild` が exit code 65 で落ちてセッションが張れない。
`appium.ios.xcode_org_id` を確認する。

### 5. identifier が実機で露出しているか ← **方式の生死はここ**

手順 4 の出力に、付けた identifier が並ぶかを見る。

```
画面に出ている identifier (resource-id):
  ✓ login_email_field
  ✓ login_submit_button
  ✓ login_title
```

**通過条件**: 付けた identifier がすべて `✓` で出る。

`scan-app` には出るのに `inspect` に出ない場合、**ソースには書かれているが実機の
アクセシビリティツリーには現れていない**ということ。この方式の前提が崩れるので、
先に進まずに原因を切り分ける。

- Android で出ない → `container: true` の書き漏れ、またはビルドの入れ替え忘れ
- iOS で出ない → Flutter の実機セマンティクスの制約に当たっている可能性がある
  ([関連する報告](https://github.com/flutter/flutter/issues/151238))。
  ここで止まる場合は `appium-flutter-integration-driver` への切り替えを検討する

### 6. 操作できるか

`tests/e2e/test_smoke.py` を手で書いて 1 本流す。

```python
import pytest
from tests.pages import LoginPage


@pytest.mark.e2e
def test_tc_smoke_001(driver, platform):
    """TC_SMOKE_001: 対象画面が表示され、ボタンをタップできる"""
    login = LoginPage(driver, platform)
    assert login.is_title_displayed()
    login.tap_submit_button()
```

```bash
uv run e2e gen-pages
uv run pytest tests/e2e/test_smoke.py --platform android -v
```

**通過条件**: green になり、`artifacts/local/evidence/TC_SMOKE_001/` に
スクリーンショットが残る。

### 7. ここまで通ったらワークフローへ

方式が実機で成立することを確認できたので、対象画面の残りの要素に
`Semantics` を付け、設計書を取り込んでワークフローを回す。
手順 6 のスモークテストは役目を終えたので消してよい。

## 実行前の準備(毎回)

アプリのビルドと端末への投入、Appium サーバの起動はハーネスの外にあるので、
先に済ませておく。

### 1. アプリをビルドして端末に入れる

**IDE のビルド/実行ボタンで入れてよい。** ハーネスはアプリを自分でインストール
しない。端末に入っているアプリを `appPackage` / `bundleId` から起動するだけなので、
どうやって入れたかは問わない。

| | 使うもの | 注意 |
|---|---|---|
| Android | Android Studio の実行ボタン | debug で問題ない |
| iOS | Xcode の実行ボタン | **スキームを Release にすること**(下記) |

コマンドで入れることもできる。その場合は次のとおり。

```bash
cd <アプリのルート>

# Android
flutter build apk --debug --target-platform android-arm64 --split-per-abi
adb install -r build/app/outputs/flutter-apk/app-arm64-v8a-debug.apk

# iOS
flutter build ios --release
```

> `e2e.config.yaml` の `build.android_apk` / `build.ios_app` は、Appium に
> インストールまでさせたい場合にだけ使う。IDE で入れる運用では参照されず、
> `doctor` でも「任意」扱いになる。Xcode の出力先は DerivedData なので、
> IDE ビルドではそもそもこのパスに成果物は出ない。

**アプリを直したら必ず入れ直すこと。** IDE の実行ボタンは毎回入れ直すので
通常は問題ないが、コマンドで入れる場合は Appium が同一バージョンの
再インストールをスキップする。「直したはずの不具合でテストが落ち続ける」
「付けたはずの identifier が実行時に見つからない」という症状が出たら、
まずここを疑う。

### 2. Appium サーバを起動する

別ターミナルで起動したままにする。

```bash
npx appium --port 4723
```

### 3. 準備できているか確認する

```bash
cd <アプリのルート>/e2e
uv run e2e doctor --platform android
```

`✗`(必須)が無ければ実行してよい。`△`(任意)と `-`(対象外)は残っていて構わない。
**アプリが端末に入っているか**は実機側を見て確認するので、IDE で入れた場合も
正しく判定される。

### iOS 実機で debug ビルドは使えない — Xcode のスキーム設定が要る

**iOS 14 以降、Flutter の debug ビルドは Flutter のツールか Xcode からしか
起動できない。** ホーム画面からも Appium からも起動できない。debug は JIT で
動くため、iOS 側の制約がかかる。

Appium はテストのたびにアプリを終了して起動し直すので、**この「起動し直し」が
debug ビルドでは失敗する。** Xcode の実行ボタンで入れた直後は動いているように
見えても、テストを流すと落ちる。

Xcode で実行ボタンを使う場合は、スキームの設定を変えておくこと。

```
Product > Scheme > Edit Scheme... > Run > Info > Build Configuration → Release
```

release で問題ないのは、本ハーネスが **Appium のネイティブドライバ
(XCUITest)を使っているから**。Flutter Driver 系は release に対応しないが、
こちらはアプリに手を入れずアクセシビリティ経由で操作するため、
リリース相当のビルドをそのまま検証できる。ドライバ選定時に狙った利点がここで効く。

release ビルドにはアプリ本体の署名も要る。Xcode で開発用の Team を設定しておくこと。

## 使い方

### 2 つの実行方式

まず前提として、**`e2e` コマンドはすべて通常のターミナルで打つ。**
これはどちらの方式でも同じ。

```
ターミナル
  └─ uv run e2e run ...                          ← 人が打つのはここだけ
       └─ copilot -p "..." --agent=e2e-...       ← e2e が内部で呼ぶ
```

**Copilot CLI に人が入力することはない。** `e2e` が内部でサブプロセスとして
起動するだけ。手動のときに人が操作するのは VS Code の Copilot Chat(GUI)で、
これも CLI ではない。

どちらになるかは **Copilot CLI の有無で自動的に決まる**。結果は `e2e run` の
出力の「実行方式」に表示される。`--mode cli` / `--mode manual` で明示もできる。

| | 自動(Copilot CLI) | 手動(VS Code のチャット) |
|---|---|---|
| 条件 | `copilot` コマンドがある | 無い(VS Code の拡張のみ) |
| `e2e` を打つ場所 | ターミナル | ターミナル |
| AI 工程 | `e2e` が `copilot` を内部で呼ぶ | プロンプトを書き出して停止。人がチャットに貼る |
| 止まる回数 | **2 回**(人のレビューのみ) | **7 回**(AI 工程 5 + レビュー 2) |
| ゲート失敗時 | 内容を添えて自動で再試行(最大 3 回) | 指摘をプロンプトに載せて再発行。人が再実行 |

どちらも**同じプロンプト資産**を使うので、生成される成果物に違いはない。

### 共通の入口

```bash
# 設計書を取り込む(Excel / Confluence から)
uv run e2e ingest ~/Downloads/ログイン機能_設計書.xlsx

# ワークフローを開始する
uv run e2e run --spec specs/ログイン機能_設計書.md --platform android
```

### 自動(Copilot CLI)の流れ

人のレビュー地点まで自動で進む。

```bash
uv run e2e status          # 進捗を見る
uv run e2e approve review  # 試験項目を確認して承認
uv run e2e resume          # 続行
uv run e2e approve confirm # 結果を確認して完了
```

### 手動(VS Code のチャット)の流れ

AI 工程ごとに止まる。止まるたびに次の 4 手順を繰り返す。

1. **ターミナルで** `e2e run`(初回)または `e2e resume`(2 回目以降)を実行する
2. 表示されたプロンプトのファイルを開く
3. **VS Code で** Copilot Chat を開き、表示されたエージェントを選んで貼り付けて実行する
4. 生成物が保存されたことを確認し、**ターミナルに戻って** 1 に戻る

つまりターミナルと VS Code を往復する。止まるたびに次のように案内が出る。

```
[1/8] 仕様の正規化 (spec)
  ! 人による実行待ちで停止します。
    1. VS Code の Copilot Chat を開く
    2. エージェント選択で `e2e-spec-analyst` を選ぶ
    3. 次のファイルの内容を貼り付けて実行する
         artifacts/<run-id>/prompts/spec.prompt.md
    4. 終わったら、このターミナルに戻って `e2e resume` を実行する
```

**この停止は失敗ではない。** 終了コードは 0 で、`e2e status` では `⏸` と表示される。

生成物が検証ゲートを通らなかった場合は、再発行されるプロンプトの冒頭に
**「前回の検証で落ちた内容」として指摘が入る**ので、それを直して貼り直す。

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
uv run e2e scan-app         # アプリのソースの Semantics(identifier:) を走査
uv run e2e inspect          # 実機の画面を取得し identifier の露出を確認
uv run e2e gen-pages        # レジストリから Page Object を再生成
uv run e2e gate <工程>       # 特定工程の検証ゲートだけ実行
uv run e2e resume --run <ID> # 過去の実行を再開
uv run e2e sync-agents      # プロンプトからエージェント定義を再生成
```

プロンプト(`prompts/`)を直すたびに
`e2e sync-agents --output ../.github/agents` を実行する。
エージェント定義は生成物なので、直接編集しても次の同期で上書きされる。

## アプリ側に必要な対応

テストで触る要素には `Semantics` による identifier の付与が必要。

```dart
Semantics(
  container: true,
  identifier: 'login_submit_button',
  child: ElevatedButton(...),
)
```

**この付与はワークフローの `locators` 工程が自動で行う。** 手で書く必要はない。
`e2e-locator-curator` は**アプリのソースを編集してよい唯一のエージェント**で、
試験項目が参照する要素にラップを追加する。

`container: true` が無いと兄弟ノードとマージされ、identifier がネイティブ側に
露出しなくなる。この挙動は実測で確認済みで、詳細と検証結果は
[`docs/phase0-findings.md`](docs/phase0-findings.md) にある。

### AI にアプリのソースを書かせるうえでの歯止め

これはこのハーネスで最もリスクの高い操作なので、3 つで抑えている。

- **範囲の限定** — 追加してよいのは `Semantics` のラップだけ。既存のロジック・
  レイアウト・命名・整形には触れない。差分が「ラップが増えただけ」に見える状態を保つ
- **`flutter analyze`** — 編集でアプリを壊していないかを工程のゲートで確認する。
  ただし実アプリは元から解析エラーを抱えていることがあるため、**工程の開始前に
  基準を取り、増えた分だけ**を落とす。既存のエラーで止まることはない
- **人のレビュー** — 自動コミットはしない。工程の完了時に差分の確認を促す

```bash
git -C <アプリのルート> diff --stat
```

変更した箇所の一覧は `artifacts/<run-id>/locator_proposal.md` に残るので、
差分を読む際の入口として使える。

工程の開始前にアプリ側の未コミット変更があれば警告する。自分の変更と AI の
変更が混ざると差分を読み分けられなくなるため、先にコミットしておくとよい。

`e2e scan-app` が `container: true` の書き漏れを検出し、`locators` 工程の
検証ゲートがブロックするため、見落としたまま先に進むことはない。

ただし `scan-app` が見るのは**ソースコードだけ**で、実機のアクセシビリティツリーに
実際に現れるかまでは分からない。両者が食い違うのがこの方式で最も厄介な失敗の形なので、
実機では `e2e inspect` で突き合わせる。

## 動作確認の記録

このハーネスは、専用に用意した検証用 Flutter アプリ(ログイン画面・ホーム画面)に
対して全 8 工程を通し、以下を実測で確認したうえで作られている。検証用アプリ自体は
リポジトリには含めていない。

- **AST 検証** — 存在しない Page Object の import と存在しないメソッド呼び出しを、
  行番号付きで検出する
- **走査ゲート** — `container: true` の書き漏れを 9 件中 1 件だけ正確に摘出する
- **スキーマゲート** — 不正な列挙値などを位置情報付きで検出する
- **実行と証跡** — 試験項目 7 件が通過。失敗時には
  `failure.png` / `page_source.xml` / `logcat.txt` / `recording.mp4` が
  試験項目 ID ごとのディレクトリに揃う
- **停止と再開** — 人のレビュー地点(工程 3・8)で停止し、`e2e approve` で再開する

### 未検証の範囲

**この確認はすべて Android エミュレータと iPhone シミュレータで行っている。
実機での実行は未検証。** 運用は実機で行うため、最初の適用時は次を確かめること。

- **Android 実機** — 差分は小さい。`udid` での端末選択と、端末側の USB デバッグ許可
- **iOS 実機** — 差分が大きい。WebDriverAgent の署名が新たに必要になり、
  ここが通らないとセッションが張れない。さらに Flutter には
  [実機でセマンティクスの一部が期待どおり出ない報告](https://github.com/flutter/flutter/issues/151238)
  がある。この報告は `semanticsLabel` に関するもので、本ハーネスが依存する
  `identifier` とは別の経路だが、**同じ領域なので実機で最初に確認すべき点**

iOS 実機は 1 画面・1 要素で `e2e scan-app` と単純なタップまで通してから、
本格的に展開することを勧める。

## リポジトリ構成

```
├── e2e.config.yaml          # 端末ごとの設定はここだけ
├── prompts/                 # エージェントのプロンプト本体(単一ソース)
├── agents.yaml              # frontmatter のメタ情報
├── .github/agents/          # 生成物。Copilot が読む
├── schemas/                 # 工程間の契約(JSON Schema)
├── specs/                   # 人が書く設計書 (SPEC_TEMPLATE.md が雛形)
├── locators/registry.yaml   # ロケータの単一ソース
├── testdata/accounts.yaml   # テストアカウントの単一ソース
├── src/e2e_harness/         # オーケストレータ
├── tests/
│   ├── conftest.py          # 証跡の自動取得
│   ├── pages/               # base.py 以外は生成物
│   ├── setup/               # 前提条件のセットアップ(AI 生成)
│   └── e2e/                 # AI 生成テスト
└── artifacts/<run-id>/      # 成果物・証跡・状態 (git 管理外)
```
