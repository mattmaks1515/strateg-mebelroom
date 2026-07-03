# Запуск стратег-бота на VPS (24/7)

Бот крутится постоянно на том же не-российском сервере, где ПМ-бот — отдельным
сервисом, в отдельной папке. Ниже по шагам. Команды копируются целиком.

> ⚠ Секреты (токены, вебхук, ключ) вписываются ТОЛЬКО в `.env` на сервере.
> В GitHub и в этот файл реальные значения НЕ попадают.

## 0. Что понадобится
- Новый Telegram-бот от **@BotFather** → его токен (отдельный от ПМ-бота).
- Тот же **ANTHROPIC_API_KEY**, что у ПМ-бота.
- (Для этапа B2) **новый** вебхук Битрикса — старый из репо ПМ-бота скомпрометирован, перевыпусти.

## 1. Подключиться к серверу
На своём ПК открой **PowerShell** и введи (подставь IP того же сервера, где ПМ-бот):
```
ssh root@ТВОЙ_IP
```

## 2. Скачать код
Репозиторий публичный (секретов в нём нет — `.env` в `.gitignore`):
```
apt update && apt install -y git python3
git clone https://github.com/mattmaks1515/strateg-mebelroom.git /root/strateg-mebelroom
```
*(Приватный репо — скажи, дам вариант с токеном доступа.)*

## 3. Прописать секреты
```
cp /root/strateg-mebelroom/.env.example /root/strateg-mebelroom/.env
nano /root/strateg-mebelroom/.env
```
Заполни (значения — свои реальные; НЕ копируй из-под «звёздочек» ••••, иначе в файл
попадут спецсимволы и будет ошибка кодировки):
```
TELEGRAM_BOT_TOKEN=<токен нового бота от BotFather>
OWNER_USER_ID=419922364,590487361
ANTHROPIC_API_KEY=<тот же ключ, что у ПМ-бота>
ANTHROPIC_MODEL=claude-fable-5
ANTHROPIC_MAX_TOKENS=4096
# BITRIX_WEBHOOK_URL пока пустой — понадобится на этапе B2
```
Сохранить в nano: `Ctrl+O`, `Enter`, затем `Ctrl+X`.

## 4. Запустить сервис
```
cd /root/strateg-mebelroom && bash deploy/setup.sh
```
Статус `active (running)` — бот работает.

## 5. Проверить
Живые логи:
```
journalctl -u strateg-bot -f
```
Напиши боту в Telegram — в логах увидишь обработку, в чат придёт ответ.
Выйти из логов — `Ctrl+C` (сервис продолжает работать).

## Обновление и управление
- Обновить код (после правок мозга/кода):
  ```
  cd /root/strateg-mebelroom && git pull && systemctl restart strateg-bot
  ```
- Стоп / старт: `systemctl stop strateg-bot` / `systemctl start strateg-bot`
- Статус: `systemctl status strateg-bot`

`.env` создаётся только на сервере и в гит не попадает — секреты остаются у тебя.
Два бота (ПМ и стратег) на одном сервере не конфликтуют: разные токены, разные папки, разные сервисы.
