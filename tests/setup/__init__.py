"""前提条件のセットアップ。

試験項目の `preconditions` に書かれた状態を、テスト本体が始まる前に
作るための処理を置く。ログイン、規約同意、ディープリンク起動など。

**identifier を付けただけではテストは動かない。** identifier は画面上の
要素を見つけるためのもので、その画面に到達する手段は別に要る。ここがその手段。

## 規約

`preconditions[].id` が `logged_in` なら、`tests/setup/logged_in.py` に
次の形の関数を置く。名前は `setup_<id>` で固定する。検証ゲートがこの名前で
実装の有無を確認するため、変えると実装漏れとして落ちる。

```python
def setup_logged_in(driver, platform, *, login_id: str, password: str) -> None:
    home = HomePage(driver, platform)
    if home.is_title_displayed():
        return  # 既にログイン済み

    login = LoginPage(driver, platform)
    login.input_login_id_field(login_id)
    login.input_password_field(password)
    login.tap_submit_button()
    home.wait_for_title()
```

- 第 1・第 2 引数は `driver` と `platform` に固定する
- `preconditions[].params` の値はキーワード引数で受ける
- **要素の操作は Page Object 経由で行う。** ロケータ文字列を直接書かない
- 同じ前提を機能ごとに作り直さない。既にあるものは再利用する
- **何度呼ばれても同じ結果になるように書く。** 冒頭で今の状態を確かめ、
  既に満たしていればすぐ返す。`no_reset: true` にすると状態が次のテストへ
  引き継がれ、これが無いと 2 件目以降が壊れる
- 状態の判定には `is_<要素>_displayed()` を使う。要素が無いときに例外ではなく
  `False` が返るため

ここに置いた関数は、このパッケージから import できるようにしておくこと。
"""

__all__: list[str] = []
