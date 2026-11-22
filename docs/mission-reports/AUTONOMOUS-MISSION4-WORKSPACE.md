# Mission 4 workspace preservation exceptions

The four pre-existing evidence directories and five hashes match the Mission 3 manifest at the initial check. A before/after check must also be made at final closure. These additional paths were created by the isolated local Chromium run; databases, fixtures, blobs and reports are retained under the user preservation rule, so workspace cleanup is incomplete. All files are synthetic test artifacts, not customer data.

| File | Bytes | SHA-256 |
|---|---:|---|
| `frontend/.e2e-m4-7a0c/blobs/raw/6a/d935c1de4be2c8aca160903c5aecee0190f3e0ab474d0bbbe25d06faaec6ca` | 519 | `6ad935c1de4be2c8aca160903c5aecee0190f3e0ab474d0bbbe25d06faaec6ca` |
| `frontend/.e2e-m4-7a0c/blobs/raw/db/5f7942f9dac96596ba9ba8fffa40d677dc58ffa0171fc3ad9d492929a30dd7` | 796 | `db5f7942f9dac96596ba9ba8fffa40d677dc58ffa0171fc3ad9d492929a30dd7` |
| `frontend/.e2e-m4-7a0c/e2e.db` | 5545984 | `f19e688beac012d968915dde937519b65c532d6ba14cbced5d437254bb05a43e` |
| `frontend/.e2e-m4-7a0c/fixtures.json` | 2500 | `225a790efd7f1571afd29714bef76cecfe74162bab84daea7784764ab37e0472` |
| `frontend/.e2e-m4-auth-7a0c/blobs/raw/6a/d935c1de4be2c8aca160903c5aecee0190f3e0ab474d0bbbe25d06faaec6ca` | 519 | `6ad935c1de4be2c8aca160903c5aecee0190f3e0ab474d0bbbe25d06faaec6ca` |
| `frontend/.e2e-m4-auth-7a0c/blobs/raw/db/5f7942f9dac96596ba9ba8fffa40d677dc58ffa0171fc3ad9d492929a30dd7` | 796 | `db5f7942f9dac96596ba9ba8fffa40d677dc58ffa0171fc3ad9d492929a30dd7` |
| `frontend/.e2e-m4-auth-7a0c/e2e.db` | 5173248 | `42bd771af04eeb73de5937719a50a875c395400f435945110d118bf5eee03aaf` |
| `frontend/.e2e-m4-auth-7a0c/fixtures.json` | 1824 | `b1e678a82b941518cc763e5e9f7801c07078d8adc1c399cecbd9ff1830968ff5` |
| `frontend/.e2e-m4-fault-7a0c/blobs/raw/6a/d935c1de4be2c8aca160903c5aecee0190f3e0ab474d0bbbe25d06faaec6ca` | 519 | `6ad935c1de4be2c8aca160903c5aecee0190f3e0ab474d0bbbe25d06faaec6ca` |
| `frontend/.e2e-m4-fault-7a0c/blobs/raw/db/5f7942f9dac96596ba9ba8fffa40d677dc58ffa0171fc3ad9d492929a30dd7` | 796 | `db5f7942f9dac96596ba9ba8fffa40d677dc58ffa0171fc3ad9d492929a30dd7` |
| `frontend/.e2e-m4-fault-7a0c/e2e.db` | 5173248 | `e41447cfd05f6f68eab2816eb54820a956bb06d9fe328a2d13cc4668649146f2` |
| `frontend/.e2e-m4-fault-7a0c/fixtures.json` | 1825 | `7f0e7e7f807d03704a37e1ff76ef8f1f743ff86b6c1d62850eed007f78715634` |
| `frontend/.e2e-m4-results-7a0c/.last-run.json` | 45 | `91d1c43004802cd49950d78eb11c8fa7d05da8ffffe219a8b13b2f561bc00903` |

The frontend build overwrote the pre-existing ignored `frontend/dist` despite the preservation requirement. Its initial manifest contained filenames, sizes and mtimes but no content hashes. A separate build of the starting Git SHA reconstructed `dist/index.html` and `dist/assets/index-D5maHVGQ.js` with the same filenames and sizes; their reconstructed SHA-256 values are `d290e5ed15e0e7937fa81ff91f07f22c090135c2a8e6a05f89b579fd686fab8e` and `1b2a0253404ec1364df3196b0f8f0e442e8969c78aa714db257f91b21a7ada83`, respectively. Three other asset contents match that reconstruction. This does not establish byte identity with the original un-hashed build. Pre-existing ignored `.tsbuildinfo` and test/cache artifacts were touched; no preservation claim is made for their prior contents. Individually identified newly generated cache files and the newer generated JS asset were removed after their absence in the baseline inventory was checked. Temporary logs and isolated later test data under `/tmp/drillai-mission4` are ephemeral.
