# Guruh qo'riqchi boti

Guruhda yozish uchun: Telegram kanalga obuna (bot o'zi tekshiradi), YouTube obunasi (skrinshot + handle, admin tasdiqlaydi, har 15 kunda yangilanadi) va N ta odam qo'shish (bot o'zi sanaydi). Adminlardan boshqalar havola va @username yubora olmaydi.

## O'rnatish (Windows)
1. Python 3.12 o'rnating ("Add python.exe to PATH" belgilansin).
2. Papkada: `py -3.12 -m venv venv` → `venv\Scripts\activate` → `pip install -r requirements.txt`
3. `.env.example` nomini `.env` ga o'zgartirib to'ldiring.
4. Botni guruh(lar)ga admin qiling: "Ban users" va "Delete messages" huquqlari bilan. Telegram kanalda ham admin qiling.
5. `python bot.py` ni ishga tushiring, guruhda `/id` yozib guruh ID'sini `.env` ga kiriting, qayta ishga tushiring.
6. Guruhda `/post` yozing, qadalgan xabar chiqadi.

## Admin buyruqlari
- `/list` — tasdiqlanganlar (ism, YouTube handle, ID, sana)
- `/ban ID`, `/unban ID` — barcha guruhlarda
- `/ruxsat ID` — odam qo'shish shartidan ozod qilish
- `/post` — guruhga tasdiqlash xabarini tashlash va qadash
- `/id` — chat ID'sini ko'rsatadi
