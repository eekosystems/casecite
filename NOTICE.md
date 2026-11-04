# Third-party notices

CaseCite
Copyright 2026 Thomas Unise

CaseCite is licensed under the Apache License 2.0 (see `LICENSE`). It is built
on the open-source packages below, each distributed under its own license.
This file lists every package and its license; it does not reproduce the
license texts. Those ship with the packages themselves: inside the Docker
image under each Python package's `*.dist-info` directory, and in each
frontend package's repository (linked below).

This file is generated — do not edit it by hand. Regenerate it whenever
`backend/requirements.lock` or `frontend/package-lock.json` changes:

```bash
python3 scripts/generate_notice.py
```

No dependency is distributed under GPL, AGPL, SSPL, BUSL, Commons Clause or
non-commercial terms. `psycopg2-binary` is LGPL and is used unmodified as a
dynamically linked library. PyMuPDF (AGPL) is deliberately not a dependency
(see `backend/requirements.txt`).

## Python (backend image)

142 packages pinned in `backend/requirements.lock`. The lock is
universal, so it includes a few packages that install only on some platforms.

| Package | Version | License |
|---|---|---|
| [aiofiles](https://pypi.org/project/aiofiles/) | 23.2.1 | Apache-2.0 |
| [aiohappyeyeballs](https://github.com/aio-libs/aiohappyeyeballs) | 2.7.1 | PSF-2.0 |
| [aiohttp](https://github.com/aio-libs/aiohttp) | 3.14.5 | Apache-2.0 AND MIT |
| [aiosignal](https://github.com/aio-libs/aiosignal) | 1.4.0 | Apache 2.0 |
| [aiosqlite](https://pypi.org/project/aiosqlite/) | 0.20.0 | MIT License |
| [alembic](https://alembic.sqlalchemy.org) | 1.13.1 | MIT |
| [annotated-doc](https://github.com/fastapi/annotated-doc) | 0.0.4 | MIT |
| [annotated-types](https://github.com/annotated-types/annotated-types) | 0.7.0 | MIT License |
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | 2.3.0 | Apache-2.0 |
| [anyio](https://pypi.org/project/anyio/) | 4.14.2 | MIT |
| [async-timeout](https://github.com/aio-libs/async-timeout) | 5.0.1 | Apache 2 |
| [asyncpg](https://pypi.org/project/asyncpg/) | 0.29.0 | Apache License, Version 2.0 |
| [attrs](https://pypi.org/project/attrs/) | 26.1.0 | MIT |
| [bcrypt](https://pypi.org/project/bcrypt/) | 5.0.0 | Apache-2.0 |
| [build](https://pypi.org/project/build/) | 1.6.1 | MIT |
| [certifi](https://github.com/certifi/python-certifi) | 2026.10.23 | MPL-2.0 |
| [cffi](https://pypi.org/project/cffi/) | 2.1.0 | MIT-0 |
| [charset-normalizer](https://pypi.org/project/charset-normalizer/) | 3.4.9 | MIT |
| [chromadb](https://github.com/chroma-core/chroma) | 1.5.9 | Apache Software License |
| [click](https://github.com/pallets/click/) | 8.4.2 | BSD-3-Clause |
| [colorama](https://github.com/tartley/colorama) | 0.4.6 | BSD License |
| [cryptography](https://pypi.org/project/cryptography/) | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| [distro](https://github.com/python-distro/distro) | 1.9.0 | Apache License, Version 2.0 |
| [dnspython](https://pypi.org/project/dnspython/) | 2.9.0 | ISC |
| [docstring-parser](https://pypi.org/project/docstring-parser/) | 0.18.0 | MIT |
| [durationpy](https://github.com/icholy/durationpy) | 0.10 | MIT |
| [email-validator](https://github.com/JoshData/python-email-validator) | 2.3.0 | Unlicense |
| [et-xmlfile](https://foss.heptapod.net/openpyxl/et_xmlfile) | 2.0.0 | MIT |
| [fastapi](https://github.com/fastapi/fastapi) | 0.162.0 | MIT |
| [filelock](https://github.com/tox-dev/py-filelock) | 4.2.4 | MIT |
| [flatbuffers](https://google.github.io/flatbuffers/) | 25.12.19 | Apache 2.0 |
| [frozenlist](https://github.com/aio-libs/frozenlist) | 1.8.0 | Apache-2.0 |
| [fsspec](https://github.com/fsspec/filesystem_spec) | 2026.6.0 | BSD-3-Clause |
| [google-auth](https://github.com/googleapis/google-cloud-python/tree/main/packages/google-auth) | 2.62.0 | Apache 2.0 |
| [google-genai](https://github.com/googleapis/python-genai) | 2.33.0 | Apache-2.0 |
| [googleapis-common-protos](https://github.com/googleapis/google-cloud-python/tree/main/packages/googleapis-common-protos) | 1.75.0 | Apache 2.0 |
| [greenlet](https://greenlet.readthedocs.io) | 3.5.3 | MIT AND PSF-2.0 |
| [grpcio](https://grpc.io) | 1.82.1 | Apache-2.0 |
| [h11](https://github.com/python-hyper/h11) | 0.16.0 | MIT |
| [hf-xet](https://github.com/huggingface/xet-core) | 1.5.1 | Apache-2.0 |
| [httpcore](https://www.encode.io/httpcore/) | 1.0.9 | BSD-3-Clause |
| [httpcore2](https://github.com/pydantic/httpx2) | 2.16.0 | BSD-3-Clause |
| [httptools](https://github.com/MagicStack/httptools) | 0.8.0 | MIT |
| [httpx](https://github.com/encode/httpx) | 0.28.1 | BSD-3-Clause |
| [httpx2](https://github.com/pydantic/httpx2) | 2.16.0 | BSD-3-Clause |
| [httpx2-jsfetch](https://pypi.org/project/httpx2-jsfetch/) | 1.0 | BSD-3-Clause |
| [huggingface-hub](https://github.com/huggingface/huggingface_hub) | 1.23.0 | Apache-2.0 |
| [idna](https://github.com/kjd/idna) | 3.18 | BSD-3-Clause |
| [importlib-resources](https://github.com/python/importlib_resources) | 7.1.0 | Apache-2.0 |
| [jiter](https://github.com/pydantic/jiter/) | 0.18.0 | MIT |
| [jsonschema](https://github.com/python-jsonschema/jsonschema) | 4.26.0 | MIT |
| [jsonschema-specifications](https://github.com/python-jsonschema/jsonschema-specifications) | 2025.9.1 | MIT |
| [kubernetes](https://github.com/kubernetes-client/python) | 36.0.2 | Apache License Version 2.0 |
| [lxml](https://lxml.de/) | 6.1.1 | BSD-3-Clause |
| [mako](https://www.makotemplates.org/) | 1.4.3 | MIT |
| [markdown-it-py](https://github.com/executablebooks/markdown-it-py) | 4.2.0 | MIT License |
| [markupsafe](https://github.com/pallets/markupsafe/) | 3.0.3 | BSD-3-Clause |
| [mdurl](https://github.com/executablebooks/mdurl) | 0.1.2 | MIT License |
| [mmh3](https://pypi.org/project/mmh3/) | 5.2.1 | MIT License |
| [multidict](https://github.com/aio-libs/multidict) | 7.2.1 | Apache-2.0 |
| [numpy](https://numpy.org) | 1.26.4 | BSD License |
| [oauthlib](https://github.com/oauthlib/oauthlib) | 4.0.0 | BSD-3-Clause |
| [olefile](https://www.decalage.info/python/olefileio) | 0.47 | BSD |
| [onnxruntime](https://onnxruntime.ai) | 1.27.0 | MIT License |
| [openai](https://github.com/openai/openai-python) | 3.40.0 | Apache-2.0 |
| [openpyxl](https://openpyxl.readthedocs.io) | 3.1.5 | MIT |
| [opentelemetry-api](https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-api) | 1.45.1 | Apache-2.0 |
| [opentelemetry-exporter-otlp-common](https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-common) | 0.66b1 | Apache-2.0 |
| [opentelemetry-exporter-otlp-proto-common](https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-common) | 1.45.1 | Apache-2.0 |
| [opentelemetry-exporter-otlp-proto-grpc](https://github.com/open-telemetry/opentelemetry-python/tree/main/exporter/opentelemetry-exporter-otlp-proto-grpc) | 1.45.1 | Apache-2.0 |
| [opentelemetry-proto](https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-proto) | 1.45.1 | Apache-2.0 |
| [opentelemetry-sdk](https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-sdk) | 1.45.1 | Apache-2.0 |
| [opentelemetry-semantic-conventions](https://github.com/open-telemetry/opentelemetry-python/tree/main/opentelemetry-semantic-conventions) | 0.66b1 | Apache-2.0 |
| [orjson](https://pypi.org/project/orjson/) | 3.11.9 | MPL-2.0 AND (Apache-2.0 OR MIT) |
| [overrides](https://github.com/mkorpela/overrides) | 7.7.0 | Apache License, Version 2.0 |
| [packaging](https://github.com/pypa/packaging) | 26.2 | Apache-2.0 OR BSD-2-Clause |
| [pdfminer-six](https://github.com/pdfminer/pdfminer.six) | 20260107 | MIT |
| [pdfplumber](https://github.com/jsvine/pdfplumber) | 0.11.10 | MIT License |
| [pillow](https://python-pillow.org) | 13.0.0 | MIT-CMU |
| [pinecone](https://www.pinecone.io) | 5.4.2 | Apache-2.0 |
| [pinecone-plugin-inference](https://www.pinecone.io) | 3.1.0 | Apache-2.0 |
| [pinecone-plugin-interface](https://www.pinecone.io) | 0.0.7 | Apache-2.0 |
| [propcache](https://github.com/aio-libs/propcache) | 0.5.2 | Apache-2.0 |
| [protobuf](https://developers.google.com/protocol-buffers/) | 5.29.6 | 3-Clause BSD License |
| [psycopg2-binary](https://psycopg.org/) | 2.9.9 | LGPL with exceptions |
| [pyasn1](https://github.com/pyasn1/pyasn1) | 0.6.5 | BSD-2-Clause |
| [pyasn1-modules](https://github.com/pyasn1/pyasn1-modules) | 0.4.2 | BSD |
| [pybase64](https://github.com/mayeut/pybase64) | 1.4.3 | BSD-2-Clause |
| [pycparser](https://github.com/eliben/pycparser) | 3.0 | BSD-3-Clause |
| [pydantic](https://github.com/pydantic/pydantic) | 2.13.4 | MIT |
| [pydantic-core](https://github.com/pydantic/pydantic) | 2.46.4 | MIT |
| [pydantic-settings](https://github.com/pydantic/pydantic-settings) | 2.14.2 | MIT |
| [pygments](https://pygments.org) | 2.20.0 | BSD-2-Clause |
| [pyjwt](https://github.com/jpadilla/pyjwt) | 2.15.1 | MIT |
| [pyotp](https://github.com/pyotp/pyotp) | 2.9.0 | MIT License |
| [pypdf](https://github.com/py-pdf/pypdf) | 6.21.4 | BSD-3-Clause |
| [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | 5.11.0 | BSD-3-Clause, Apache-2.0, dependency licenses |
| [pypika](https://github.com/kayak/pypika) | 0.51.1 | Apache License Version 2.0 |
| [pyproject-hooks](https://github.com/pypa/pyproject-hooks) | 1.2.0 | MIT License |
| [pytesseract](https://github.com/madmaze/pytesseract) | 0.3.10 | Apache License 2.0 |
| [python-dateutil](https://github.com/dateutil/dateutil) | 2.9.0 | Dual License |
| [python-docx](https://github.com/python-openxml/python-docx) | 1.1.0 | MIT |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | 1.2.2 | BSD-3-Clause |
| [python-http-client](https://github.com/sendgrid/python-http-client) | 3.3.7 | MIT |
| [python-magic](http://github.com/ahupp/python-magic) | 0.4.27 | MIT |
| [python-magic-bin](http://github.com/julian-r/python-magic) | 0.4.14 | MIT |
| [python-multipart](https://github.com/Kludex/python-multipart) | 0.0.32 | Apache-2.0 |
| [python-pptx](https://github.com/scanny/python-pptx) | 1.0.2 | MIT |
| [pyyaml](https://pyyaml.org/) | 6.0.3 | MIT |
| [redis](https://github.com/redis/redis-py) | 8.1.0 | MIT |
| [referencing](https://github.com/python-jsonschema/referencing) | 0.37.0 | MIT |
| [regex](https://github.com/mrabarnett/mrab-regex) | 2026.7.10 | Apache-2.0 AND CNRI-Python |
| [reportlab](https://www.reportlab.com/) | 5.0.1 | BSD License |
| [requests](https://github.com/psf/requests) | 2.34.2 | Apache-2.0 |
| [requests-oauthlib](https://github.com/requests/requests-oauthlib) | 2.0.0 | ISC |
| [rich](https://github.com/Textualize/rich) | 15.0.0 | MIT |
| [rpds-py](https://github.com/crate-py/rpds) | 2026.6.3 | MIT |
| [sendgrid](https://github.com/sendgrid/sendgrid-python/) | 6.11.0 | MIT |
| [shellingham](https://github.com/sarugaku/shellingham) | 1.5.4 | ISC License |
| [six](https://github.com/benjaminp/six) | 1.17.0 | MIT |
| [sniffio](https://github.com/python-trio/sniffio) | 1.3.1 | MIT OR Apache-2.0 |
| [sqlalchemy](https://www.sqlalchemy.org) | 2.0.28 | MIT |
| [starkbank-ecdsa](https://github.com/starkbank/ecdsa-python.git) | 2.3.1 | MIT License |
| [starlette](https://github.com/Kludex/starlette) | 1.3.1 | BSD-3-Clause |
| [striprtf](https://github.com/joshy/striprtf) | 0.0.33 | BSD-3-Clause |
| [tenacity](https://github.com/jd/tenacity) | 8.2.3 | Apache 2.0 |
| [tiktoken](https://pypi.org/project/tiktoken/) | 0.14.0 | MIT |
| [tokenizers](https://github.com/huggingface/tokenizers) | 0.23.1 | Apache Software License |
| [tqdm](https://pypi.org/project/tqdm/) | 4.68.4 | MPL-2.0 AND MIT |
| [truststore](https://github.com/sethmlarson/truststore) | 0.10.5 | MIT |
| [typer](https://github.com/fastapi/typer) | 0.26.8 | MIT |
| [typing-extensions](https://github.com/python/typing_extensions) | 4.16.0 | PSF-2.0 |
| [typing-inspection](https://github.com/pydantic/typing-inspection) | 0.4.2 | MIT |
| [urllib3](https://pypi.org/project/urllib3/) | 2.8.0 | MIT |
| [uvicorn](https://www.uvicorn.org/) | 0.27.0 | BSD License |
| [uvloop](https://pypi.org/project/uvloop/) | 0.22.1 | MIT License |
| [watchfiles](https://github.com/samuelcolvin/watchfiles) | 1.2.0 | MIT |
| [websocket-client](https://github.com/websocket-client/websocket-client.git) | 1.9.0 | Apache-2.0 |
| [websockets](https://github.com/python-websockets/websockets) | 14.2 | BSD-3-Clause |
| [xlrd](http://www.python-excel.org/) | 2.0.2 | BSD |
| [xlsxwriter](https://github.com/jmcnamara/XlsxWriter) | 3.2.9 | BSD-2-Clause |
| [yarl](https://github.com/aio-libs/yarl) | 1.25.1 | Apache-2.0 |

## JavaScript (frontend bundle)

45 production packages in `frontend/package-lock.json`.

| Package | Version | License |
|---|---|---|
| [@azure/msal-browser](https://www.npmjs.com/package/@azure/msal-browser) | 3.30.0 | MIT |
| [@azure/msal-common](https://www.npmjs.com/package/@azure/msal-common) | 14.16.1 | MIT |
| [@fontsource/inter](https://www.npmjs.com/package/@fontsource/inter) | 5.3.0 | OFL-1.1 |
| [@fontsource/jetbrains-mono](https://www.npmjs.com/package/@fontsource/jetbrains-mono) | 5.3.0 | OFL-1.1 |
| [@fontsource/source-serif-4](https://www.npmjs.com/package/@fontsource/source-serif-4) | 5.3.0 | OFL-1.1 |
| [ansi-regex](https://www.npmjs.com/package/ansi-regex) | 5.0.1 | MIT |
| [ansi-styles](https://www.npmjs.com/package/ansi-styles) | 4.3.0 | MIT |
| [camelcase](https://www.npmjs.com/package/camelcase) | 5.3.1 | MIT |
| [cliui](https://www.npmjs.com/package/cliui) | 6.0.0 | ISC |
| [color-convert](https://www.npmjs.com/package/color-convert) | 2.0.1 | MIT |
| [color-name](https://www.npmjs.com/package/color-name) | 1.1.4 | MIT |
| [cookie](https://www.npmjs.com/package/cookie) | 1.1.1 | MIT |
| [decamelize](https://www.npmjs.com/package/decamelize) | 1.2.0 | MIT |
| [dijkstrajs](https://www.npmjs.com/package/dijkstrajs) | 1.0.3 | MIT |
| [emoji-regex](https://www.npmjs.com/package/emoji-regex) | 8.0.0 | MIT |
| [find-up](https://www.npmjs.com/package/find-up) | 4.1.0 | MIT |
| [get-caller-file](https://www.npmjs.com/package/get-caller-file) | 2.0.5 | ISC |
| [is-fullwidth-code-point](https://www.npmjs.com/package/is-fullwidth-code-point) | 3.0.0 | MIT |
| [js-tokens](https://www.npmjs.com/package/js-tokens) | 4.0.0 | MIT |
| [locate-path](https://www.npmjs.com/package/locate-path) | 5.0.0 | MIT |
| [loose-envify](https://www.npmjs.com/package/loose-envify) | 1.4.0 | MIT |
| [lucide-react](https://www.npmjs.com/package/lucide-react) | 0.344.0 | ISC |
| [p-limit](https://www.npmjs.com/package/p-limit) | 2.3.0 | MIT |
| [p-locate](https://www.npmjs.com/package/p-locate) | 4.1.0 | MIT |
| [p-try](https://www.npmjs.com/package/p-try) | 2.2.0 | MIT |
| [path-exists](https://www.npmjs.com/package/path-exists) | 4.0.0 | MIT |
| [pngjs](https://www.npmjs.com/package/pngjs) | 5.0.0 | MIT |
| [qrcode](https://www.npmjs.com/package/qrcode) | 1.5.4 | MIT |
| [react](https://www.npmjs.com/package/react) | 18.3.1 | MIT |
| [react-dom](https://www.npmjs.com/package/react-dom) | 18.3.1 | MIT |
| [react-router](https://www.npmjs.com/package/react-router) | 7.18.4 | MIT |
| [react-router-dom](https://www.npmjs.com/package/react-router-dom) | 7.18.4 | MIT |
| [require-directory](https://www.npmjs.com/package/require-directory) | 2.1.1 | MIT |
| [require-main-filename](https://www.npmjs.com/package/require-main-filename) | 2.0.0 | ISC |
| [scheduler](https://www.npmjs.com/package/scheduler) | 0.23.2 | MIT |
| [set-blocking](https://www.npmjs.com/package/set-blocking) | 2.0.0 | ISC |
| [set-cookie-parser](https://www.npmjs.com/package/set-cookie-parser) | 2.7.3 | MIT |
| [string-width](https://www.npmjs.com/package/string-width) | 4.2.3 | MIT |
| [strip-ansi](https://www.npmjs.com/package/strip-ansi) | 6.0.1 | MIT |
| [which-module](https://www.npmjs.com/package/which-module) | 2.0.1 | ISC |
| [wrap-ansi](https://www.npmjs.com/package/wrap-ansi) | 6.2.0 | MIT |
| [y18n](https://www.npmjs.com/package/y18n) | 4.0.3 | ISC |
| [yargs](https://www.npmjs.com/package/yargs) | 15.4.1 | MIT |
| [yargs-parser](https://www.npmjs.com/package/yargs-parser) | 18.1.3 | ISC |
| [zustand](https://www.npmjs.com/package/zustand) | 5.0.11 | MIT |

## System packages (Docker image)

The image is built on `python:3.11-slim` (Debian) and installs nginx, curl,
gosu, libmagic, libffi and tesseract-ocr from the Debian archive. Each is
distributed under its own license; the copyright and license files are inside
the image at `/usr/share/doc/<package>/copyright`.

## Fonts

Inter, JetBrains Mono and Source Serif 4 are distributed under the SIL Open
Font License 1.1.
