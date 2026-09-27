# Hardware — podklady

## Dotyk: XPT2046 (klon TI ADS7846)

- `XPT2046.pdf` — XPTEK 2007 (zdroj: https://www.waveshare.com/w/upload/b/b0/XPT2046.pdf)
- `ADS7846.pdf` — TI SBAS125H (zdroj: https://www.ti.com/lit/ds/symlink/ads7846.pdf)

Co z nich plyne pro `ytdj/panel/kedei.c` (zjištěno 27. 9. 2026, prst u okraje četl
o 35–113 px vedle a pokaždé jinak):

- **Touch Screen Settling (XPT2046 s. 17, ADS7846 „TOUCH SCREEN SETTLING")**: když se
  panel nestihne ustálit, projeví se to jako chyba zesílení. Správně je buď zpomalit
  DCLK (akvizice jsou jen 3 takty po řídicím bajtu), nebo nechat budiče zapnuté
  (PD0 = 1), udělat několik převodů a power-down poslat až u posledního — to platí pro
  X, Y i Z.
- **Řídicí bajt**: `S A2 A1 A0 MODE SER/DFR PD1 PD0`; PD1:PD0 = 00 power-down mezi převody
  s povoleným PENIRQ, 01 ADC zapnutý (PENIRQ vypnutý), 11 stále zapnuto. PENIRQ funguje
  jen v power-down s PD0 = 0 — poslední příkaz musí být s PD = 00.
- **Časy**: tACQ min 1,5 µs; DCLK max 2 MHz (propustnost 125 kHz); DCLK high/low min 200 ns.
- **Odpor dotyku**: R = R_Xplate · (X/4096) · (Z2/Z1 − 1). Slabý tlak (prst u rámečku) =
  velký odpor = delší ustalování.
