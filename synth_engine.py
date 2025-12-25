import math
import array
import uctypes
import rp2
from machine import Pin, mem32
from dual_dac import dual_dac_bus, encoder_pusher

class Synth:
    def __init__(self, sample_rate=44100):
        self.sr = sample_rate

    def _normalize(self, v):
        return int(max(0, min(255, 127.5 + (v * 127.5))))

    def sine(self, freq, duration_sec, amp=1.0):
        samples = int(self.sr * duration_sec)
        buf = bytearray(samples)
        for i in range(samples):
            t = i / self.sr
            v = math.sin(2 * math.pi * freq * t) * amp
            buf[i] = self._normalize(v)
        return buf

    def square(self, freq, duration_sec, duty_percent=50, amp=1.0):
        samples = int(self.sr * duration_sec)
        buf = bytearray(samples)
        period = int(self.sr / freq)
        high_samples = int(period * (duty_percent / 100.0))
        for i in range(samples):
            v = 1.0 if (i % period) < high_samples else -1.0
            buf[i] = self._normalize(v * amp)
        return buf

    def saw(self, freq, duration_sec, amp=1.0):
        samples = int(self.sr * duration_sec)
        buf = bytearray(samples)
        period = self.sr / freq
        for i in range(samples):
            v = 2.0 * ((i / period) - math.floor(0.5 + i / period))
            buf[i] = self._normalize(v * amp)
        return buf
        
    def tri(self, freq, duration_sec, amp=1.0):
        samples = int(self.sr * duration_sec)
        buf = bytearray(samples)
        period = self.sr / freq
        for i in range(samples):
            t = (i % period) / period
            v = 2 * abs(2 * t - 1) - 1
            buf[i] = self._normalize(v * amp)
        return buf

class DacPair:
    def __init__(self, sm_id, pin_base, pin_wr, dma_base):
        # Pins: Base (Data 0-7), WR (Side 0), CS_Sig (Side 1), CS_Amp (Side 2)
        # Note: sideset_base argument sets the FIRST pin of the sideset group.
        # We expect WR, CS_Sig, CS_Amp to be contiguous starting at pin_wr.
        
        self.sm = rp2.StateMachine(sm_id, dual_dac_bus, freq=1_000_000, 
                                   out_base=Pin(pin_base), 
                                   sideset_base=Pin(pin_wr))
        self.sm.active(1)
        
        self.DMA_BASE = 0x50000000
        self.ch_data = dma_base
        self.ch_conf_cnt = dma_base + 1
        self.ch_conf_addr = dma_base + 2
        
        # Loop Control Block in RAM: [Next_Addr, Next_Count]
        self.ctrl_block = array.array('I', [0, 0])
        self.ctrl_block_addr = uctypes.addressof(self.ctrl_block)
        self.current_buf = None # Prevent GC

    def _dma_map(self, ch, offset):
        return self.DMA_BASE + (ch * 0x40) + offset

    def play(self, interleaved_buf, sample_rate):
        # 2 bytes per sample (Sig + Amp) * 2 cycles per byte = 4 cycles/sample
        req_freq = sample_rate * 4
        # Clamp for 200ns write cycle limit (approx 10MHz max SM clock)
        if req_freq > 10_000_000: req_freq = 10_000_000
        
        self.sm.init(dual_dac_bus, freq=req_freq, 
                     out_base=self.sm.out_base, sideset_base=self.sm.sideset_base)
        self.sm.active(1)

        # Setup Buffers
        self.current_buf = interleaved_buf
        buf_addr = uctypes.addressof(interleaved_buf)
        count = len(interleaved_buf) # Words? No, PIO pulls bytes. DREQ is byte based?
        # Actually, we usually push words to FIFO and let PIO pull.
        # But for simplicity, let's transfer Bytes to PIO TX.
        
        # Init Control Block
        self.ctrl_block[0] = buf_addr
        self.ctrl_block[1] = count

        pio_tx_addr = 0x50200000 + (0x10 if self.sm.id() < 4 else 0x20) # Offset for PIO0 vs PIO1
        # Handle specific PIO offsets if using PIO1... (Simplified for PIO0 SM 0-3)
        if self.sm.id() == 0: pio_tx_addr = 0x50200010
        elif self.sm.id() == 1: pio_tx_addr = 0x50200014
        
        # --- DMA CONFIG ---
        # CH1: Config Count -> Writes to CH0 TRANS_COUNT
        c1_ctrl = (self.ch_conf_addr << 11) | (0x3f << 15) | (2 << 2) | 1
        mem32[self._dma_map(self.ch_conf_cnt, 0x00)] = self.ctrl_block_addr + 4
        mem32[self._dma_map(self.ch_conf_cnt, 0x04)] = self._dma_map(self.ch_data, 0x08)
        mem32[self._dma_map(self.ch_conf_cnt, 0x08)] = 1
        mem32[self._dma_map(self.ch_conf_cnt, 0x0C)] = c1_ctrl

        # CH2: Config Addr -> Writes to CH0 READ_ADDR_TRIG (0x3C)
        c2_ctrl = (0x3f << 11) | (0x3f << 15) | (2 << 2) | 1
        mem32[self._dma_map(self.ch_conf_addr, 0x00)] = self.ctrl_block_addr
        mem32[self._dma_map(self.ch_conf_addr, 0x04)] = self._dma_map(self.ch_data, 0x3C)
        mem32[self._dma_map(self.ch_conf_addr, 0x08)] = 1
        mem32[self._dma_map(self.ch_conf_addr, 0x0C)] = c2_ctrl

        # CH0: Data Pump -> PIO TX
        # DREQ: PIO0_TX0=0, PIO0_TX1=1 (Matches SM ID for PIO0)
        treq = self.sm.id()
        c0_ctrl = (self.ch_conf_cnt << 11) | (treq << 15) | (0 << 2) | 1 # 0<<2 = Byte size
        
        mem32[self._dma_map(self.ch_data, 0x00)] = buf_addr
        mem32[self._dma_map(self.ch_data, 0x04)] = pio_tx_addr
        mem32[self._dma_map(self.ch_data, 0x08)] = count
        mem32[self._dma_map(self.ch_data, 0x0C)] = c0_ctrl | 1 # Start

    def next_buffer(self, interleaved_buf):
        self.current_buf = interleaved_buf # Keep alive
        self.ctrl_block[0] = uctypes.addressof(interleaved_buf)
        self.ctrl_block[1] = len(interleaved_buf)

    def update_power(self, power_level):
        if self.current_buf:
            # Interleaved format: S, P, S, P...
            # Power is at odd indices: 1, 3, 5...
            m = memoryview(self.current_buf)
            m[1::2] = bytes([int(power_level)]) * (len(m) // 2)

class PioEncoder:
    def __init__(self, sm_id, pin_a, pin_b):
        # Pin B is unused in 'in_base' but needed for wiring
        self.sm = rp2.StateMachine(sm_id, encoder_pusher, freq=10_000_000, in_base=Pin(pin_a))
        self.sm.active(1)
        self.val = 0
        self._last_state = 0
        # 00->01(+1), 01->11(+1), 11->10(+1), 10->00(+1)
        self._table = [0, -1, 1, 0, 1, 0, 0, -1, -1, 0, 0, 1, 0, 1, -1, 0] 
        
    def get_delta(self):
        delta = 0
        while self.sm.rx_fifo() > 0:
            # FIFO contains 32-bit words, but we only pushed 2 bits
            new_state = self.sm.get() & 0b11
            idx = (self._last_state << 2) | new_state
            delta += self._table[idx]
            self._last_state = new_state
        return delta