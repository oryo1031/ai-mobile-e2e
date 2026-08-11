# Phase 0 実現性検証 — 結果

実施日: 2026-08-12

## 結論

**ネイティブドライバ方式(UiAutomator2 / XCUITest + `Semantics(identifier:)`)は成立する。**
Phase 1 以降へ進んでよい。`appium-flutter-integration-driver` への切替は不要。

ただし **identifier を付ければ必ず露出するわけではない**。マージを回避する書き方が
必須で、これを守らないと静かに identifier が消える。下の「付与ガイドライン」が
本フェーズの最重要成果物。

> **注意: この検証はエミュレータとシミュレータで行っている。**
> 実運用は Android 実機・iOS 実機で行うため、本書の結論のうち
> 「identifier がネイティブ側に露出する」という前提は実機で再確認すること。
> 特に iOS 実機は、WebDriverAgent の署名が新たに必要になるうえ、Flutter に
> [実機でセマンティクスの一部が期待どおり出ない報告](https://github.com/flutter/flutter/issues/151238)
> がある(報告は `semanticsLabel` に関するもので、本方式が使う `identifier` とは
> 別経路だが、同じ領域のため)。
> 付与ガイドラインとロケータ戦略の結論自体は、実機でも変わらない見込み。

## 検証環境

| 項目 | 値 |
|---|---|
| Flutter | 3.38.9 (Dart 3.10.8) |
| Appium | 3.6.0 |
| uiautomator2 driver | 8.2.2 |
| xcuitest driver | 12.3.1 |
| Appium-Python-Client | 6.x (Python 3.12.13) |
| Android | Pixel_9 エミュレータ |
| iOS | iPhone 16 Pro シミュレータ (iOS 18.5) |

検証アプリは使い捨てのスパイク(`flutter_probe`)。23個の identifier を
ウィジェット種別ごとに付与し、両プラットフォームで露出と操作可否を実測した。

## 露出結果: 両プラットフォームで 19/23、成否パターンは完全一致

Android の `resource-id` と iOS の `accessibilityIdentifier` は、**どのウィジェットが
成功しどれが失敗するかが完全に一致した**。アノテーション規則をプラットフォームごとに
分ける必要はない。

### 成功したもの

静的テキスト / 標準ボタン / テキスト入力 / アイコンのみボタン / チェックボックス /
スイッチ / カスタム描画(`CustomPaint`) / 動的リスト項目(インデックス付き) /
AppBar タイトル / 画面遷移先の要素。

`CustomPaint` のような完全独自描画でも identifier を付ければ露出する点は重要。
Flutter が何を描いているかに関係なく、a11y ノードとして掴める。

### 失敗したもの — すべて「セマンティクスのマージ」が原因

`Card` の中に複数の子を並べ、各子を素の `Semantics(identifier:)` で包んだケース:

- 先頭の子(`probe_card_title`)だけがノードとして残り、その `content-desc` /
  `label` が **`'カード見出し\nカード本文'` と連結された**
- 2番目以降の子(`probe_card_subtitle`)は identifier ごと消滅
- 子の `TextButton`(`probe_card_action`)はノード自体は残るが
  **`resource-id` が空**になり、外側の identifier が伝播しなかった

### 対策の実測比較

同じ構造に対して3つの書き方を並べて比較した結果:

| 書き方 | 結果 |
|---|---|
| `Semantics(identifier:)` のみ | **NG** — 隣接ノードとマージされ identifier が失われる |
| `Semantics(container: true, identifier:)` | **OK** — 3要素すべて個別に露出 |
| 親に `Semantics(explicitChildNodes: true)` | **NG** — 効果なし。マージは防げない |
| `MergeSemantics` + `Semantics(button: true, identifier:)` | **OK** — ボタンとして正しく露出 |

`identifier` を設定すると暗黙的に `container: true` 相当になる、という説明が
ドキュメント周辺にあるが、**実測では成り立たなかった**。`container: true` は明示が必要。

## 付与ガイドライン(アプリ側チームへの依頼内容)

```dart
// 必ずこの形で書く。container: true を省略しない。
Semantics(
  container: true,
  identifier: 'screen_element_role',
  child: TargetWidget(...),
)
```

- `container: true` を **常に明示する**。省略すると兄弟ノードとマージされ、
  identifier が予告なく消える
- `explicitChildNodes` はマージ対策にならないので使わない
- ボタンなど操作要素は `MergeSemantics` + `Semantics(button: true, identifier:)`
  でも良い
- リストなど動的要素は `identifier: 'xxx_item_$index'` のようにインデックスを含める
  (実測で一意に振られることを確認済み)
- identifier を付けていない要素は a11y ツリーに一切出ない(対照群で確認済み)。
  テストで触る要素には漏れなく付与が必要

## ロケータ戦略(プラットフォームで異なる)

**Android は `AppiumBy.ID` が使えない。** Appium が bare な id にパッケージ名を
補完してしまい、Flutter が出す接頭辞なしの resource-id と一致しないため。

| プラットフォーム | 戦略 | 可否 |
|---|---|---|
| Android | `AppiumBy.ID`(bare / pkg 付きとも) | **NG** |
| Android | `AppiumBy.XPATH` `//*[@resource-id='x']` | OK |
| Android | `AppiumBy.ANDROID_UIAUTOMATOR` `resourceId("x")` | OK |
| iOS | `AppiumBy.ACCESSIBILITY_ID` | OK |
| iOS | `AppiumBy.XPATH` `//*[@name='x']` | OK |
| iOS | `AppiumBy.IOS_PREDICATE` `name == 'x'` | OK |

→ `tools/gen_pages.py` は **プラットフォーム別にロケータを解決する Page Object**
を生成する必要がある。テストコード側は identifier 名だけを扱い、戦略の差は
Page Object の内側に隠す。

## 操作可否

Android・iOS 両方で、identifier 経由で掴んだ要素に対する以下の操作が成功した。

- ボタンのタップと、その結果の状態変化の検証(カウンタが 0 → 1)
- テキストフィールドへの日本語入力と入力値の読み取り
- チェックボックス / スイッチのトグルと状態属性の検証
- 画面遷移と、遷移先要素の取得

## スクロールに関する重要な制約

**a11y ツリーには画面内に描画されているノードしか現れない。** 画面外の要素は
`find_element` で必ず失敗する。実際、最初の実行ではリスト下部の要素がすべて
検出できず、スクロールしながら page source を収集して初めて全 23 要素を確認できた。

→ Page Object のアクションは **scroll-into-view を既定の挙動として組み込む**。
Android は `UiScrollable.scrollIntoView`、iOS は相当のスクロール処理を使う。
これを個々のテストコードに書かせるとAIが必ず書き漏らすため、生成側で吸収する。

## 運用上の注意

- **同一バージョンの APK は再インストールされない。** Appium は署名とバージョンが
  同じだとインストールをスキップするため、アプリを変更したのに古いビルドを検証して
  しまう事故が実際に起きた。手元で回す運用ではアプリを直して再実行する頻度が高く、
  最も踏みやすい罠になる。**テスト実行の前に必ず明示的に投入すること。**

  ```bash
  flutter build apk --debug --target-platform android-arm64 --split-per-abi
  adb install -r build/app/outputs/flutter-apk/app-arm64-v8a-debug.apk
  ```

  症状は「直したはずの不具合でテストが落ち続ける」「アプリ側に付けた
  identifier が `e2e scan-app` には出るのに実行時に見つからない」という形で出る。
  ロケータ不備を疑う前に、まずビルドが入れ替わっているかを確認する
- Appium サーバのプロセスに `ANDROID_HOME` が必要。クライアント側に設定しても効かない
- Flutter の debug APK は 150MB あり、エミュレータのストレージを圧迫した。
  `--split-per-abi --target-platform android-arm64` で 70MB に落として回避した

## 次のアクション

1. アプリ側リポジトリに、上記ガイドラインに沿って `Semantics(container: true,
   identifier:)` を付与するブランチを作る(対象は最初にE2E化する1画面のみ)
2. Phase 1(実行基盤)へ進む
