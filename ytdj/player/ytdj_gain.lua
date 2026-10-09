-- ytdj: srovnání hlasitosti skladeb (docs/FUNKCE.md F-ZVUK-24, F-ZVUK-25).
--
-- Každá skladba dostane vlastní pevný zisk jako filtr ve svém řetězci zvuku
-- (file-local-options/af), nasazený před jejím prvním vzorkem (on_preloaded).
-- Proč filtr a ne `volume-gain` / replaygain: ty mpv násobí až při čtení ze
-- zásoby výstupu (--audio-buffer=2), takže by zisk nové skladby dopadl i na
-- poslední ~2 s té předchozí. Filtr je před zásobou — změna je přesně na
-- hranici skladeb a po skladbě ho mpv samo zahodí (volba jen pro soubor).
-- Hlasitost (vlastnost `volume`) se tu nikdy nečte ani nemění.
--
-- Kolik: zisk spočítal resolver (ytdl_loudness.gain_db) a připsal ho na konec
-- JSONu pro mpv; ytdl_hook (mpv ≥ 0.38) ten JSON nechává ve vlastnosti
-- user-data/mpv/ytdl/json-subprocess-result. Starší mpv ho tam nemá — pro ně
-- ytdj posílá zisk zprávou (script-message-to ytdj_gain gain <videoId> <dB>
-- <LUFS>), jakmile ho resolver ohlásí. Skript sám nic nepočítá a nezávisí na
-- tom, jestli zrovna odpovídá hlavní smyčka ytdj.
--
-- Co nasadil, ohlásí zprávou `ytdj-gain <videoId> <dB> <LUFS> <odkud> <stav>
-- [lift]` (ytdj z ní píše událost track.gain; „lift" = přizvednuté tiché pasáže).
--
-- Pojistka: kdyby mpv filtr neumělo postavit (jiné ffmpeg), skladba by vůbec
-- nezazněla ("no audio or video data played"). Taková skladba se podruhé
-- pustí bez filtru (ytdj ji načte znovu) a po dvou takových selháních za
-- sebou se srovnávání do restartu vypne — radši různě hlasitě než ticho.

local LABEL = "ytdjgain"
-- Zesílení jen přes omezovač špiček na −1 dBFS (level=disabled: žádné
-- dorovnání hlasitosti, jen strop), ať tichá nahrávka se špičkami u nuly
-- nepřebuzuje. Ztlumení omezovač nepotřebuje.
-- latency=1: bez zpoždění o dobu náběhu (jinak 5 ms ticha na začátku a
-- useknutých 5 ms na konci zesílené skladby). Omezovač počítá v double —
-- aformat vrátí float jako u ostatních skladeb, ať se výstup (ao) neotevírá
-- v jiném formátu podle toho, která skladba hrála po startu první.
local LIMITER = "alimiter=limit=0.891:attack=5:release=50:level=disabled:latency=1,"
    .. "aformat=sample_fmts=fltp"
local MAX_CUT, MAX_BOOST = 24, 12 -- pojistka; skutečné meze drží resolver

-- Přizvednutí tichých pasáží (F-ZVUK-32, nastavení loudness_lift_quiet; výchozí
-- vypnuto = nic z toho se nestaví). Za pevný zisk skladby se přidá souběžná
-- komprese: k signálu se přimíchá jeho silně stlačená kopie (mix 1/3, makeup 4:
-- výstup = vstup × (2/3 + 4/3 × útlum kompresoru)), takže pasáž hluboko pod
-- cílovou hlasitostí zesílí nejvýš o 6,5 dB, hlasitá skoro vůbec a ticho
-- zůstane tichem — žádné neomezené zesilování šumu. Práh je vztažený k cíli
-- srovnání (LIFT_THR platí pro −12 LUFS a posouvá se s cílem), LIFT_POST
-- dorovná hlasité části zpět na cíl. Vždy přes omezovač špiček.
-- Měření na 18 skladbách: docs/FUNKCE.md F-ZVUK-32.
local LIFT_THR, LIFT_REF, LIFT_POST = 0.07, -12, 0.5
-- script-opts ytdj_gain-lift / ytdj_gain-target: nastavuje ytdj při startu mpv
-- i za běhu (change-list script-opts) — platí od další načtené skladby
local opts = { lift = false, target = -14 }
-- pcall: kdyby tohle mpv volby skriptu číst neumělo, srovnání hlasitosti běží dál
pcall(function()
    require("mp.options").read_options(opts, "ytdj_gain", function() end)
end)
local KEEP = 200 -- kolik zisků ze zpráv držet

local pushed, order = {}, {}
local current = nil -- skladba, které jsme nasadili filtr: { vid, started }
local skip = {} -- videoId, kterému filtr nešel postavit — příště bez něj
local fails, broken = 0, false

mp.register_script_message("gain", function(vid, gain, lufs)
    gain = tonumber(gain)
    if not vid or not gain then
        return
    end
    if pushed[vid] == nil then
        order[#order + 1] = vid
        if #order > KEEP then
            pushed[table.remove(order, 1)] = nil
        end
    end
    pushed[vid] = { gain = gain, lufs = tonumber(lufs) }
end)

local function number_after(text, key)
    return tonumber(text:match('"' .. key .. '":%s*(-?%d+%.?%d*)'))
end

local function from_json()
    local out = mp.get_property_native("user-data/mpv/ytdl/json-subprocess-result/stdout")
    if type(out) ~= "string" or out == "" then
        return nil, nil
    end
    local tail = out:sub(-200)
    return number_after(tail, "ytdj_gain_db"), number_after(tail, "ytdj_loudness_lufs")
end

local function lift_graph()
    local target = math.max(-24, math.min(-8, tonumber(opts.target) or -14))
    local thr = LIFT_THR * 10 ^ ((target - LIFT_REF) / 20)
    return string.format("acompressor=threshold=%.4f:ratio=20:attack=20:release=2000:makeup=4"
        .. ":mix=0.3333:knee=2:detection=rms:link=average,volume=%.1fdB", thr, LIFT_POST)
end

local function apply(gain, lift)
    local list = {}
    for _, f in ipairs(mp.get_property_native("af") or {}) do
        if f.label ~= LABEL then
            list[#list + 1] = f
        end
    end
    local graph = string.format("volume=%.1fdB", gain)
    if lift then
        graph = graph .. "," .. lift_graph() .. "," .. LIMITER
    elseif gain > 0 then
        graph = graph .. "," .. LIMITER
    end
    list[#list + 1] = { name = "lavfi", label = LABEL, params = { graph = graph } }
    return mp.set_property_native("file-local-options/af", list)
end

mp.add_hook("on_preloaded", 50, function()
    local path = mp.get_property("path") or ""
    local vid = path:match("[?&]v=([%w_-]+)") or ""
    local gain, lufs = from_json()
    local via = "json"
    if gain == nil and pushed[vid] then
        gain, lufs, via = pushed[vid].gain, pushed[vid].lufs or lufs, "message"
    end
    local state = "none"
    local lift = false
    current = nil
    if gain ~= nil then
        lift = opts.lift == true
        gain = math.max(-MAX_CUT, math.min(MAX_BOOST, gain))
        if broken or skip[vid] then
            state = "broken" -- filtr tu nejde postavit: hraje se beze změny
        elseif math.abs(gain) < 0.05 and not lift then
            state = "unity" -- už je na cílové hlasitosti: žádný filtr
        elseif apply(gain, lift) then
            state = gain > 0 and "limited" or "ok"
            current = { vid = vid, started = false }
        else
            state = "unset" -- mpv volbu nepřijalo: hraje se beze změny
        end
    else
        via = "none"
    end
    local lifted = current ~= nil and lift
    local msg = { "script-message", "ytdj-gain", vid,
        gain and string.format("%.1f", gain) or "", lufs and string.format("%.2f", lufs) or "",
        via, state }
    if lifted then
        msg[#msg + 1] = "lift" -- šestý údaj jen u skladby s přizvednutými tichými pasážemi
    end
    mp.commandv((table.unpack or unpack)(msg))
end)

mp.register_event("playback-restart", function()
    if current and not current.started then
        current.started = true
        fails = 0
    end
end)

mp.register_event("end-file", function(event)
    local cur = current
    current = nil
    if cur and not cur.started and event.reason == "error" then
        skip[cur.vid] = true
        fails = fails + 1
        if fails >= 2 then
            broken = true
        end
        mp.commandv("script-message", "ytdj-gain", cur.vid, "", "", "none",
            broken and "disabled" or "failed")
    end
end)
