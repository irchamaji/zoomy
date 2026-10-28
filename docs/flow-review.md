# Review flow Zoomy — 23 September 2026

Review berdasarkan kode lokal dan simulasi handler. Tidak mengirim pesan Telegram, masuk ke rapat, mengakses rekaman pengguna, atau melakukan deployment. Perubahan lokal yang sudah ada pada recorder, store, dan fitur delete dipertahankan. Snapshot perubahan sebelum review: `/tmp/zoomy-before-flow-improvements.patch` (lokal sementara).

## Alur penggunaan

1. Mulai dengan `/start` atau `/help` → `/record <link atau Meeting ID>`.
2. Isi passcode jika diperlukan → pilih nama bot → nama rekaman.
3. Tinjau konfigurasi → Start Now atau Schedule (WIB).
4. Bot bergabung; pengguna memantau melalui `/status`, `/ongoing`, `/peek`, dan menghentikan dengan `/stop`.
5. Buka `/history [kata kunci]` → pilih rekaman → transkripsi, ringkasan/percakapan AI, berbagi, rename, atau delete dengan konfirmasi.
6. Gunakan `/cancel` untuk keluar dari input sementara. Rekaman aktif dan jadwal tersimpan tetap berjalan.

## Masalah yang diperbaiki

| Temuan | Dampak | Perbaikan |
|---|---|---|
| `/record` hanya membaca argumen pertama | Meeting ID dengan spasi ditolak | Gabungkan seluruh argumen sebelum parsing |
| `/status` menunjuk `/ongoing` yang belum diregistrasikan | Pengguna menemui menu bantuan alih-alih detail | Registrasikan handler yang sudah tersedia |
| Tidak ada pembatalan universal | Setup atau mode AI menahan input berikutnya | `/cancel` membersihkan state sementara, timer, dan tombol |
| Mode input dapat bertumpuk | Nama/passcode bisa dikonsumsi sebagai konteks AI atau rename | Tolak pembukaan input lain dengan petunjuk menyelesaikan flow atau `/cancel` |
| Tombol lama memakai state terbaru | Klik pada menu lama bisa memulai setup atau menarget rekaman lain | Periksa identitas pengguna, chat, pesan, dan tahap setup sebelum dispatch callback |
| Delete belum mengikat konfirmasi ke folder | Konfirmasi yang tak lagi sesuai bisa menghapus pilihan terbaru | Simpan folder yang diminta untuk dihapus dan cocokkan saat konfirmasi |
| Ganti nama bot dan konfirmasi sesi tambahan tidak memiliki timeout | Setup dapat menggantung | Pasang timeout 100 detik; nama kembali ke default, konfirmasi sesi tambahan dibatalkan |
| Tidak ada ringkasan sebelum Start Now | Sulit memeriksa konfigurasi | Tampilkan ID rapat, nama bot/rekaman, status passcode, dan zona waktu |
| Nama rekaman tidak divalidasi saat input | Nama yang sulit digunakan atau pesan terlalu panjang | Batasi nama 1–80 karakter; tolak karakter filename terlarang |
| Riwayat hanya pagination dan detail tidak memiliki Back | Sulit menemukan atau membandingkan rekaman | Pencarian nama, filter terlihat, dan Back to recordings |
| Membuka `/history` lagi menyisakan timer lama | Menu baru dapat kedaluwarsa akibat timer sebelumnya | Batalkan timer lama sebelum mengganti state |
| Pesan tanpa state diabaikan | Tidak jelas cara melanjutkan | Balas dengan petunjuk `/record`, `/history`, `/help` |
| Beberapa pesan HTML dirender sebagai teks | Entitas/tag terlihat di ringkasan jadwal dan detail rekaman | Aktifkan parse mode pada pesan terkait |

## Prioritas lanjutan

- **Konfirmasi waktu untuk jadwal baru:** flow reschedule sudah menampilkan waktu hasil parsing sebelum konfirmasi; jadwal baru masih langsung disimpan. Samakan langkah tersebut agar interpretasi bahasa alami dapat diperiksa.
- **Akses riwayat antar pengguna:** daftar saat ini mengambil seluruh folder media, sedangkan kontrol sesi aktif per pengguna. Metadata `user_id` sudah tersedia untuk rekaman baru, tetapi aturan berbagi dan migrasi rekaman lama belum ditentukan. Jangan menganggap riwayat privat per pengguna.
- **Ketahanan penyimpanan jadwal:** penulisan JSON masih langsung dan kegagalan hanya dicatat. Perlu penyimpanan atomik dan pemberitahuan kegagalan agar konfirmasi jadwal tidak menyesatkan.
- **Proses transkripsi/AI versus rename/delete:** selain pemeriksaan rekaman aktif, perlu status pekerjaan per file supaya file yang sedang diproses tidak dipindah atau dihapus.
- **Konsistensi bahasa:** antarmuka mencampur Inggris dan Indonesia. Perlu satu keputusan bahasa untuk seluruh flow, termasuk pesan dari recorder dan AI.

## Verifikasi

Hasil lokal: **20 tes lulus**, `compileall` lulus, dan `git diff --check` bersih.

Suite `tests/test_bot_flow.py` menguji handler secara offline: parsing ID, callback kadaluwarsa/lintas pengguna, pembatalan tanpa menghentikan recording/jadwal, konflik mode input, timeout, ringkasan tanpa membocorkan passcode, pencarian riwayat, navigasi kembali, dan registrasi command.

Sebelum release, lakukan smoke test Telegram pada bot uji untuk memastikan keyboard, callback alert, timeout nyata, serta satu rekaman sampai tersimpan. Suite offline tidak memverifikasi Zoom, FFmpeg/PulseAudio, layanan AI, atau perilaku Telegram live.
