# Glodex M1c 脱敏验证记录

本记录仅保存批准的脱敏 live 证据，不保存本次输入或远程内容。

| 字段 | 证据 |
|---|---|
| Provider | `DeepSeek Open Platform` |
| Request model | `deepseek-v4-flash` |
| Calls | `1` |
| Safe terminal | `COMPLETED` |
| Automatic evidence | `scripts/verify_m1c.py` exit `0`；`tests/m1c/contract/test_deepseek_http.py` 单 POST、无 retry contract；`tests/m1c/nfr/test_m1c_offline_security.py` 脱敏与 Git inventory |
