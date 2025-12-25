import time
import machine
from machine import Pin, SPI
import synth_engine
import pcd8544 # Ensure you have this library uploaded
import array

# --- Configuration ---
PIN_BUS_A_BASE = 0   # Data: 0-7, WR: 8, CS_Sig: 9, CS_Amp: 10
PIN_BUS_B_BASE = 16  # Data: 16-23, WR: 24, CS_Sig: 25, CS_Amp: 26

# UI Pins
PIN_ENC_NAV_A = 27
PIN_ENC_NAV_B = 28
PIN_ENC_NAV_SW = 29

PIN_ENC_VAL_A = 30
PIN_ENC_VAL_B = 31

PIN_LCD_CLK = 36
PIN_LCD_DIN = 35
PIN_LCD_DC  = 32
PIN_LCD_RST = 33
PIN_LCD_CE  = 34

# --- Data Models ---
class ChannelState:
    def __init__(self, name):
        self.name = name
        # Param: [Current, Min, Max, Step]
        self.params = {
            "Wave":     ["Sine", ["Sine", "Tri", "Saw", "Sqr"]], # Special case list
            "Duty":     [50, 1, 99, 1],
            "Power":    [200, 0, 255, 5],
            "Freq":     [440, 20, 10000, 10],
            "Fs":       [44100, 8000, 100000, 1000]
        }
        self.order = ["Wave", "Freq", "Power", "Duty", "Fs"]
        self.selected_idx = 0
        self.edit_mode = False
        self.last_interact = 0

# --- Helpers ---
def generate_interleaved(syn, ch_state):
    # Extract params
    w_type = ch_state.params["Wave"][0]
    freq = ch_state.params["Freq"][0]
    duty = ch_state.params["Duty"][0]
    pwr = ch_state.params["Power"][0]
    fs = ch_state.params["Fs"][0]
    
    # Duration: ensure at least 1 full cycle to loop cleanly
    # Simple approach: 0.1s buffer or calculated period
    dur = 0.1 
    
    if w_type == "Sine": buf = syn.sine(freq, dur)
    elif w_type == "Sqr": buf = syn.square(freq, dur, duty)
    elif w_type == "Tri": buf = syn.tri(freq, dur)
    elif w_type == "Saw": buf = syn.saw(freq, dur)
    else: buf = syn.sine(freq, dur) # Fallback
    
    # Interleave
    count = len(buf)
    interleaved = bytearray(count * 2)
    interleaved[0::2] = buf
    interleaved[1::2] = bytes([int(pwr)]) * count
    return interleaved

# --- Setup Hardware ---
spi = SPI(1, baudrate=4000000, polarity=0, phase=0, 
          sck=Pin(PIN_LCD_CLK), mosi=Pin(PIN_LCD_DIN))
lcd = pcd8544.PCD8544(spi, cs=Pin(PIN_LCD_CE), dc=Pin(PIN_LCD_DC), rst=Pin(PIN_LCD_RST))

# Nav Encoder (Standard IRQ)
nav_delta = 0
def nav_irq(pin):
    global nav_delta
    if pin.value() != Pin(PIN_ENC_NAV_B).value(): nav_delta += 1
    else: nav_delta -= 1

p_nav_a = Pin(PIN_ENC_NAV_A, Pin.IN, Pin.PULL_UP)
p_nav_a.irq(trigger=Pin.IRQ_FALLING, handler=nav_irq)
Pin(PIN_ENC_NAV_B, Pin.IN, Pin.PULL_UP)
p_nav_btn = Pin(PIN_ENC_NAV_SW, Pin.IN, Pin.PULL_UP)

# Value Encoder (PIO)
# Note: Using SM 4 (PIO1 SM0)
enc_val = synth_engine.PioEncoder(sm_id=4, pin_a=PIN_ENC_VAL_A, pin_b=PIN_ENC_VAL_B)

# DACs
# Bus A: SM0, DMA 0-2
dac_a = synth_engine.DacPair(sm_id=0, pin_base=PIN_BUS_A_BASE, pin_wr=PIN_BUS_A_BASE+8, dma_base=0)
# Bus B: SM1, DMA 3-5
dac_b = synth_engine.DacPair(sm_id=1, pin_base=PIN_BUS_B_BASE, pin_wr=PIN_BUS_B_BASE+8, dma_base=3)

# Synth Engine
syn = synth_engine.Synth()

# App State
channels = [ChannelState("CH A"), ChannelState("CH B")]
active_ch = 0

# --- Initial Playback ---
print("Booting Synth...")
buf_a = generate_interleaved(syn, channels[0])
buf_b = generate_interleaved(syn, channels[1])

dac_a.play(buf_a, channels[0].params["Fs"][0])
dac_b.play(buf_b, channels[1].params["Fs"][0])

# --- Main Loop ---
last_render = 0
btn_prev = 1

while True:
    now = time.ticks_ms()
    
    # 1. Process Navigation
    if nav_delta != 0:
        ch = channels[active_ch]
        if not ch.edit_mode:
            ch.selected_idx = (ch.selected_idx + nav_delta) % len(ch.order)
        else:
            # Allow switching parameters even in edit mode? Or lock?
            # Spec says: "smooth encoder to set it". So Click encoder navigates.
            pass 
        nav_delta = 0
        ch.last_interact = now
        
    # 2. Process Button (Click)
    btn_curr = p_nav_btn.value()
    if btn_curr == 0 and btn_prev == 1: # Press
        # Simple toggle active channel if holding? No, stick to spec.
        ch = channels[active_ch]
        ch.edit_mode = not ch.edit_mode
        if not ch.edit_mode:
            # Exit Edit -> Save/Regen
            new_buf = generate_interleaved(syn, ch)
            target_dac = dac_a if active_ch == 0 else dac_b
            target_dac.next_buffer(new_buf)
        ch.last_interact = now
        time.sleep_ms(50) # Debounce
    btn_prev = btn_curr
    
    # 3. Process Value (Precision Enc)
    val_delta = enc_val.get_delta()
    if val_delta != 0:
        ch = channels[active_ch]
        if ch.edit_mode:
            key = ch.order[ch.selected_idx]
            p = ch.params[key]
            
            if key == "Wave":
                opts = p[1]
                curr_i = opts.index(p[0])
                new_i = (curr_i + val_delta) % len(opts)
                p[0] = opts[new_i]
            else:
                # Numeric
                new_val = p[0] + (val_delta * p[3])
                p[0] = max(p[1], min(p[2], new_val))
                
            # Instant Power Update
            if key == "Power":
                target_dac = dac_a if active_ch == 0 else dac_b
                target_dac.update_power(p[0])
                
            ch.last_interact = now
            
    # 4. Timeout Auto-Save
    ch = channels[active_ch]
    if ch.edit_mode and time.ticks_diff(now, ch.last_interact) > 5000:
        ch.edit_mode = False
        new_buf = generate_interleaved(syn, ch)
        target_dac = dac_a if active_ch == 0 else dac_b
        target_dac.next_buffer(new_buf)
        
    # 5. Render UI (30 FPS)
    if time.ticks_diff(now, last_render) > 33:
        lcd.fill(0)
        # Header
        lcd.text(f"ACT: {channels[active_ch].name}", 0, 0, 1)
        
        y = 10
        ch = channels[active_ch]
        for i, key in enumerate(ch.order):
            val = ch.params[key][0]
            sel = (i == ch.selected_idx)
            
            prefix = ">" if sel else " "
            # Draw Box if editing and selected
            if sel and ch.edit_mode:
                # Draw inverted box logic
                t = f"{key[:4]}:{str(val)}"
                lcd.fill_rect(0, y-1, 84, 9, 1) # Black box
                lcd.text(t, 6, y, 0) # White text
            else:
                lcd.text(f"{prefix}{key[:4]}:{val}", 0, y, 1)
            y += 9
            
        lcd.show()
        last_render = now