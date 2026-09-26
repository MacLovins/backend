# @maclovins/leadradar-api

TypeScript-типы для LeadRadar API. Генерируются [openapi-typescript](https://openapi-ts.dev)
из `openapi.json` в корне репо и публикуются в GitHub Packages workflow'ом
`.github/workflows/ts-types.yml` при каждом пуше в `main`, который меняет `openapi.json`.

Версия: `<major>.<minor>` из `info.version` в `openapi.json` + номер запуска CI как patch
(например `0.1.42`).

## Подключение во фронте

`.npmrc` в корне фронта (токен — GitHub PAT classic со скоупом `read:packages`):

```
@maclovins:registry=https://npm.pkg.github.com
//npm.pkg.github.com/:_authToken=${GITHUB_TOKEN}
```

```sh
npm i -D @maclovins/leadradar-api
npm i openapi-fetch
```

```ts
import createClient from "openapi-fetch";
import type { paths, components } from "@maclovins/leadradar-api";

const api = createClient<paths>({ baseUrl: "/api" });
const { data, error } = await api.GET("/health");

type Company = components["schemas"]["CompanyOut"];
```

## Локально

`make ts-types` — сгенерировать `clients/ts/index.d.ts` (в docker, без node на хосте).
