/*
 * KeDei 3.5" v6.2 (480x320, SPI) — obraz i dotyk přímo přes registry SPI0 a GPIO.
 *
 * Deska nemá D/C linku: každé slovo jsou tři bajty (0x11 00 cmd, 0x15 00 data,
 * 0x15 hi lo pixel) a kolem KAŽDÉHO slova se musí zvednout T_CS (GPIO8) a po
 * něm zase shodit — jinak displej nic nezobrazí. Přes spidev to znamená pár
 * syscallů na pixel (~17–34 µs), celá obrazovka pak trvá sekundy. Tady se
 * obě linky CS přepínají zápisem do registru, takže pixel stojí ~1 µs.
 *
 * Kernelový ovladač SPI proto nesmí běžet (dtparam=spi=off): piny 7–11 si
 * nastavujeme sami a sběrnici sdílíme s dotykem (XPT2046 na T_CS, PENIRQ na
 * GPIO25) — obsluhuje obojí jeden proces, takže se nic nepřetahuje.
 *
 * Potřebuje /dev/mem, tedy roota. Adresy jsou pro BCM2837 (Pi 3).
 */
#include <fcntl.h>
#include <stdint.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>

#define PERI_BASE 0x3F000000u
#define GPIO_BASE (PERI_BASE + 0x200000u)
#define SPI0_BASE (PERI_BASE + 0x204000u)

/* GPIO registry (indexy do uint32_t) */
#define GPFSEL0 0
#define GPSET0 7
#define GPCLR0 10
#define GPLEV0 13

/* SPI0 registry */
#define SPI_CS 0
#define SPI_FIFO 1
#define SPI_CLK 2

#define CS_CLEAR_TX (1u << 4)
#define CS_CLEAR_RX (1u << 5)
#define CS_TA (1u << 7)
#define CS_DONE (1u << 16)
#define CS_RXD (1u << 17)
#define CS_TXD (1u << 18)

#define PIN_LCS 7   /* CE1 — chip select LCD */
#define PIN_TCS 8   /* CE0 — chip select dotyku, zároveň „strobe“ LCD */
#define PIN_MISO 9
#define PIN_MOSI 10
#define PIN_SCLK 11
#define PIN_PENIRQ 25

void kd_touch_arm(void);

static volatile uint32_t *gpio;
static volatile uint32_t *spi;
static uint32_t lcd_cdiv = 16;    /* 400 MHz / 16 = 25 MHz */
static uint32_t touch_cdiv = 256; /* ~1,5 MHz — XPT2046 zvládne max ~2,5 MHz */

static inline void barrier(void) { __sync_synchronize(); }

static void set_fsel(int pin, uint32_t fn) {
    volatile uint32_t *reg = gpio + GPFSEL0 + pin / 10;
    int shift = (pin % 10) * 3;
    *reg = (*reg & ~(7u << shift)) | (fn << shift);
}

static void sleep_us(long us) {
    struct timespec ts = {us / 1000000, (us % 1000000) * 1000};
    nanosleep(&ts, NULL);
}

/* Jedno slovo pro LCD: T_CS nahoru, L_CS dolů, bajty, počkat, zpátky. */
static inline void lcd_word(const uint8_t *b, int n) {
    gpio[GPSET0] = 1u << PIN_TCS;
    gpio[GPCLR0] = 1u << PIN_LCS;
    barrier();
    for (int i = 0; i < n; i++) spi[SPI_FIFO] = b[i];
    while (!(spi[SPI_CS] & CS_DONE)) {}
    spi[SPI_CS] = CS_TA | CS_CLEAR_RX; /* příjem nás nezajímá, jen ho vyprázdnit */
    barrier();
    gpio[GPSET0] = 1u << PIN_LCS;
    gpio[GPCLR0] = 1u << PIN_TCS;
    barrier();
}

static void cmd(uint8_t c) { uint8_t b[3] = {0x11, 0x00, c}; lcd_word(b, 3); }
static void dat(uint8_t d) { uint8_t b[3] = {0x15, 0x00, d}; lcd_word(b, 3); }

static void bus_lcd(void) {
    spi[SPI_CLK] = lcd_cdiv;
    spi[SPI_CS] = CS_TA | CS_CLEAR_TX | CS_CLEAR_RX; /* mode 0, TA trvale */
    barrier();
}

int kd_open(int cdiv) {
    if (cdiv > 0) lcd_cdiv = (uint32_t)cdiv & ~1u;
    int fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) return -1;
    gpio = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, GPIO_BASE);
    spi = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, SPI0_BASE);
    close(fd);
    if (gpio == MAP_FAILED || spi == MAP_FAILED) return -2;

    set_fsel(PIN_MISO, 4); /* ALT0 */
    set_fsel(PIN_MOSI, 4);
    set_fsel(PIN_SCLK, 4);
    gpio[GPSET0] = 1u << PIN_LCS;
    gpio[GPCLR0] = 1u << PIN_TCS;
    set_fsel(PIN_LCS, 1); /* výstup */
    set_fsel(PIN_TCS, 1);
    set_fsel(PIN_PENIRQ, 0); /* vstup */
    barrier();
    kd_touch_arm();
    bus_lcd();
    return 0;
}

static const uint8_t init_seq[] = {
    /* cmd, počet dat, data… */
    0xB0, 1, 0x00,
    0xB3, 4, 0x02, 0x00, 0x00, 0x00,
    0xB9, 4, 0x01, 0x00, 0x0F, 0x0F,
    0xC0, 8, 0x13, 0x3B, 0x00, 0x02, 0x00, 0x01, 0x00, 0x43,
    0xC1, 4, 0x08, 0x0F, 0x08, 0x08,
    0xC4, 4, 0x11, 0x07, 0x03, 0x04,
    0xC6, 1, 0x00,
    0xC8, 20, 0x03, 0x03, 0x13, 0x5C, 0x03, 0x07, 0x14, 0x08, 0x00, 0x21,
              0x08, 0x14, 0x07, 0x53, 0x0C, 0x13, 0x03, 0x03, 0x21, 0x00,
    0x35, 1, 0x00,
    0x36, 1, 0x60,
    0x3A, 1, 0x55,
    0x44, 2, 0x00, 0x01,
    0xD0, 4, 0x07, 0x07, 0x1D, 0x03,
    0xD1, 3, 0x03, 0x30, 0x10,
    0xD2, 3, 0x03, 0x14, 0x04,
};

void kd_init(int madctl) {
    bus_lcd();
    static const uint8_t r1[4] = {0, 1, 0, 0}, r0[4] = {0, 0, 0, 0};
    lcd_word(r1, 4); sleep_us(50000);
    lcd_word(r0, 4); sleep_us(100000);
    lcd_word(r1, 4); sleep_us(50000);

    cmd(0x00); sleep_us(10000);
    cmd(0xFF); cmd(0xFF); sleep_us(10000);
    for (int i = 0; i < 4; i++) cmd(0xFF);
    sleep_us(15000);
    cmd(0x11); sleep_us(150000);

    for (size_t i = 0; i < sizeof init_seq;) {
        cmd(init_seq[i]);
        int n = init_seq[i + 1];
        for (int k = 0; k < n; k++) dat(init_seq[i + 2 + k]);
        i += 2 + n;
    }
    cmd(0x29); sleep_us(30000);
    cmd(0xB4); dat(0x00);
    cmd(0x36); dat((uint8_t)madctl);
}

void kd_madctl(int madctl) {
    bus_lcd();
    cmd(0x36); dat((uint8_t)madctl);
}

/* Obdélník [x0..x1]×[y0..y1] včetně, px = RGB565 řádek po řádku. */
void kd_blit(int x0, int y0, int x1, int y1, const uint16_t *px) {
    bus_lcd();
    cmd(0x2A); dat(x0 >> 8); dat(x0 & 255); dat(x1 >> 8); dat(x1 & 255);
    cmd(0x2B); dat(y0 >> 8); dat(y0 & 255); dat(y1 >> 8); dat(y1 & 255);
    cmd(0x2C);
    long n = (long)(x1 - x0 + 1) * (y1 - y0 + 1);
    uint8_t w[3] = {0x15, 0, 0};
    for (long i = 0; i < n; i++) {
        w[1] = px[i] >> 8;
        w[2] = px[i] & 255;
        lcd_word(w, 3);
    }
}

/* Totéž, ale z RGB888 (bajty z Pillow, `stride` bajtů na řádek). */
void kd_blit_rgb(int x0, int y0, int x1, int y1, const uint8_t *rgb, int stride) {
    bus_lcd();
    cmd(0x2A); dat(x0 >> 8); dat(x0 & 255); dat(x1 >> 8); dat(x1 & 255);
    cmd(0x2B); dat(y0 >> 8); dat(y0 & 255); dat(y1 >> 8); dat(y1 & 255);
    cmd(0x2C);
    int w = x1 - x0 + 1, h = y1 - y0 + 1;
    uint8_t word[3] = {0x15, 0, 0};
    for (int r = 0; r < h; r++) {
        const uint8_t *p = rgb + (long)r * stride;
        for (int c = 0; c < w; c++, p += 3) {
            uint16_t v = (uint16_t)((p[0] & 0xF8) << 8 | (p[1] & 0xFC) << 3 | p[2] >> 3);
            word[1] = v >> 8;
            word[2] = v & 255;
            lcd_word(word, 3);
        }
    }
}

/* ---- dotyk ---- */

static int xpt_read(uint8_t command) {
    uint8_t tx[3] = {command, 0, 0}, rx[3];
    spi[SPI_CS] = CS_TA | CS_CLEAR_TX | CS_CLEAR_RX;
    for (int i = 0; i < 3; i++) spi[SPI_FIFO] = tx[i];
    while (!(spi[SPI_CS] & CS_DONE)) {}
    for (int i = 0; i < 3; i++) {
        while (!(spi[SPI_CS] & CS_RXD)) {}
        rx[i] = spi[SPI_FIFO];
    }
    return ((rx[1] << 8) | rx[2]) >> 3;
}

/*
 * Na dotyk jen zpomalit hodiny. T_CS (GPIO8) se tu NESMÍ přepínat: LCD si
 * slovo zapisuje na jeho sestupné hraně, takže každé zvednutí a shození navíc
 * zapíše poslední slovo znovu a posune ukazatel v paměti displeje — při
 * držení prstu se pak kus obrazu (třeba časová osa) nedokreslí. Převodník
 * vybírat nemusíme: T_CS je mezi slovy LCD dole a před každým slovem se
 * zvedne, čímž se převodník sám vrátí do výchozího stavu.
 */
static void bus_touch(void) {
    spi[SPI_CLK] = touch_cdiv;
    barrier();
}

int kd_pen_down(void) { return !(gpio[GPLEV0] & (1u << PIN_PENIRQ)); }

/* Převodník do power-down s povoleným PENIRQ (PD1:0 = 00). Po zapnutí může
   být v jiném režimu a pak by se o dotyku nikdy nedozvěděl. */
void kd_touch_arm(void) {
    bus_touch();
    xpt_read(0x80);
    bus_lcd();
}

/* Diagnostika: změří bez ohledu na PENIRQ. */
void kd_touch_force(int *x, int *y, int *z1, int *z2) {
    bus_touch();
    *x = xpt_read(0xD0);
    *y = xpt_read(0x90);
    *z1 = xpt_read(0xB0);
    *z2 = xpt_read(0xC0);
    xpt_read(0x80);
    bus_lcd();
}

static void sort_int(int *a, int n) {
    for (int i = 1; i < n; i++)
        for (int j = i; j > 0 && a[j - 1] > a[j]; j--) { int t = a[j]; a[j] = a[j - 1]; a[j - 1] = t; }
}

/*
 * Změří dotyk: vrátí 1 a surové x, y (0–4095) a tlak z, nebo 0 když se nikdo
 * nedotýká. Mediány z několika vzorků, protože odporová vrstva šumí.
 */
int kd_touch(int *x, int *y, int *z) {
    if (!kd_pen_down()) return 0;
    bus_touch();

    enum { N = 7 };
    int xs[N], ys[N], z1 = 0, z2 = 0;
    xpt_read(0xD0); /* první převod po výběru bývá mimo */
    for (int i = 0; i < N; i++) {
        xs[i] = xpt_read(0xD0);
        ys[i] = xpt_read(0x90);
    }
    z1 = xpt_read(0xB0);
    z2 = xpt_read(0xC0);
    xpt_read(0x80); /* zpět do power-down s povoleným PENIRQ */
    int still = kd_pen_down();
    bus_lcd();
    if (!still) return 0; /* prst se zvedl uprostřed měření — hodnoty jsou smetí */

    sort_int(xs, N);
    sort_int(ys, N);
    /* PENIRQ občas cukne i bez prstu a převodník pak vrátí klidové hodnoty
       (x≈0, y≈4095, z1≈0) — po kalibraci pravý dolní roh, tedy tlačítko
       hlasitosti. Skutečný dotyk má z1 v řádu stovek a osy mimo dorazy. */
    if (z1 < 60 || xs[N / 2] < 40 || ys[N / 2] > 4050) return 0;
    /* medián nesmí stát na rozptýlených vzorcích (prst dosedá/zvedá se) */
    if (xs[N - 2] - xs[1] > 150 || ys[N - 2] - ys[1] > 150) return 0;
    *x = xs[N / 2];
    *y = ys[N / 2];
    *z = z1 + 4095 - z2;
    return 1;
}
