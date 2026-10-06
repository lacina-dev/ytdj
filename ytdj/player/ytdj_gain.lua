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
-- Co nasadil, ohlásí zprávou `ytdj-gain <videoId> <dB> <LUFS> <odkud> <stav>`
-- (ytdj z ní píše událost track.gain).
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

local function apply(gain)
    local list = {}
    for _, f in ipairs(mp.get_property_native("af") or {}) do
        if f.label ~= LABEL then
            list[#list + 1] = f
        end
    end
    local graph = string.format("volume=%.1fdB", gain)
    if gain > 0 then
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
    current = nil
    if gain ~= nil then
        gain = math.max(-MAX_CUT, math.min(MAX_BOOST, gain))
        if broken or skip[vid] then
            state = "broken" -- filtr tu nejde postavit: hraje se beze změny
        elseif math.abs(gain) < 0.05 then
            state = "unity" -- už je na cílové hlasitosti: žádný filtr
        elseif apply(gain) then
            state = gain > 0 and "limited" or "ok"
            current = { vid = vid, started = false }
        else
            state = "unset" -- mpv volbu nepřijalo: hraje se beze změny
        end
    else
        via = "none"
    end
    mp.commandv("script-message", "ytdj-gain", vid,
        gain and string.format("%.1f", gain) or "", lufs and string.format("%.2f", lufs) or "",
        via, state)
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
