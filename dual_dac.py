

import rp2

# --- Dual DAC Driver ---
# Data: 8 bits (OUT pins)
# Control: 3 bits (SIDE-SET pins) -> WR, CS_Sig, CS_Amp
# Mapping: 
#   Side-set bit 0: WR
#   Side-set bit 1: CS_Sig
#   Side-set bit 2: CS_Amp
# Active Low Logic:
#   Idle:    WR=1, CS1=1, CS2=1 -> 0b111 (7)
#   Wr_Sig:  WR=0, CS1=0, CS2=1 -> 0b100 (4)
#   Wr_Amp:  WR=0, CS1=1, CS2=0 -> 0b010 (2)

@rp2.asm_pio(
    out_init=(rp2.PIO.OUT_LOW,) * 8,
    sideset_init=(rp2.PIO.OUT_HIGH,)*3,
    fifo_join=rp2.PIO.JOIN_TX,
    autopull=True,
    pull_thresh=16 
)
def dual_dac_bus():
    wrap_target()
    # 1. Output Signal Byte (Even byte)
    out(pins, 8)    .side(4)   # Set Data, Drop WR & CS_Sig
    nop()           .side(7)   # Raise WR & CS_Sig (Latch)
    
    # 2. Output Amp/Power Byte (Odd byte)
    out(pins, 8)    .side(2)   # Set Data, Drop WR & CS_Amp
    nop()           .side(7)   # Raise WR & CS_Amp (Latch)
    wrap()

# --- Precision Encoder Driver ---
# Monitors 2 pins. Pushes raw 2-bit state to FIFO on ANY change.
# CPU uses lookup table to determine direction.
# This guarantees no missed steps even if CPU is busy.

@rp2.asm_pio(in_shiftdir=rp2.PIO.SHIFT_LEFT, out_shiftdir=rp2.PIO.SHIFT_RIGHT)
def encoder_pusher():
    wrap_target()
    mov(osr, pins)      # Load current pin state
    out(x, 2)           # Save to X (only bottom 2 bits)
    
    label("loop")
    mov(isr, pins)      # Read new state
    in_(null, 30)       # Clear garbage
    mov(y, isr)         # Save to Y
    jmp(x_not_y, "change")
    jmp("loop")
    
    label("change")
    in_(y, 2)           # Push new state to ISR
    push(noblock)       # Send to FIFO
    mov(x, y)           # Update Last State
    wrap()