# Скиллы Korra

Открытые скиллы для ИИ-агентов от команды [Korra](https://korra-agent.online).
Каждый скилл — папка с `SKILL.md` и скриптами в формате
[Agent Skills](https://agentskills.io). Способы установки и степень проверки
для Claude Code, Codex, Cursor, OpenClaw, Hermes и Korra указаны на странице каждого скилла.

Open agent skills by the Korra team (Agent Skills format; instructions in Russian).

| Скилл | Что делает | Страница |
|---|---|---|
| [mac-care](mac-care/) | Удалённый аудит Mac и согласованные настройки силами агента; кандидат 0.1.0, живая приёмка впереди | [Библиотека Korra](https://korra-agent.online/library/mac-care/) |
| [telegram-emoji-packs](telegram-emoji-packs/) | Эмодзи-паки и стикерпаки Telegram через своего бота: из своих картинок и видео или из чужих паков; правки, порядок, превью | [Библиотека Korra](https://korra-agent.online/library/telegram-emoji-packs/) |

## Установка

Проще всего отправить агенту ссылку на страницу скилла в
[Библиотеке Korra](https://korra-agent.online/library/): там готовое задание
и команды для каждого агента.

Для telegram-emoji-packs Korra и Hermes используют штатную установку:

```bash
korra skills install ceremoneymeister-bit/korra-skills/telegram-emoji-packs --yes
# или: hermes skills install ceremoneymeister-bit/korra-skills/telegram-emoji-packs --yes
```

Claude Code, Codex, Cursor, OpenClaw — скопировать папку скилла в папку
скиллов агента:

```bash
git clone --depth 1 https://github.com/ceremoneymeister-bit/korra-skills.git /tmp/korra-skills
mkdir -p ~/.claude/skills && cp -r /tmp/korra-skills/telegram-emoji-packs ~/.claude/skills/   # Claude Code
mkdir -p ~/.agents/skills && cp -r /tmp/korra-skills/telegram-emoji-packs ~/.agents/skills/   # Codex, Cursor, OpenClaw
```

## Mac Care: доступность

Открыт исходный кандидат 0.1.0. Для Korra подготовлен официальный optional-пакет
`devops/mac-care`: он станет доступен после включения в выпуск коробки.
Обычная community-установка из GitHub в проверенной Korra блокируется сканером
из-за управления SSH-доступом. Не обходите эту проверку ручным копированием.
Совместимость с другими агентами и живой сеанс на Mac ещё не приняты;
[результаты проверок](mac-care/references/evaluation.md).

## Лицензия

[MIT](LICENSE).
