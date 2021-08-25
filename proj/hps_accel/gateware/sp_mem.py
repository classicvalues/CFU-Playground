# Copyright 2021 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from migen.fhdl.structure import Mux
from nmigen import unsigned
from nmigen.hdl.ast import Shape, Signal
from nmigen.hdl.dsl import Module
from nmigen.hdl.mem import Memory
from nmigen.hdl.rec import DIR_FANOUT, Layout
from nmigen_cfu.util import SimpleElaboratable, ValueBuffer

from .stream import Endpoint


class MemoryParameters:
    """Defines a memory.

    Also has convenience functions for defining the Streams used in the
     Memory interface.
    """

    def __init__(self, *, width, depth):
        """
        Args:
          width: int
            Width of each memory word
          depth: int
            Number of words stored in the memory.
        """
        self.width = width
        self.depth = depth

    def addr_shape(self) -> Shape:
        """nMigen shape describing the address."""
        return unsigned((self.depth - 1).bit_length())

    def data_shape(self) -> Shape:
        """nMigen shape describing a data word."""
        return unsigned(self.width)

    def write_stream_payload_type(self) -> Layout:
        """Gets the payload type for a write stream"""
        return Layout([
            ("addr", self.addr_shape(), DIR_FANOUT),
            ("data", self.data_shape(), DIR_FANOUT),
        ])

    def make_write_stream(self):
        return Endpoint(self.write_stream_payload_type(), src_loc_at=1)

    def make_read_addr_stream(self):
        return Endpoint(self.addr_shape(), src_loc_at=1)

    def make_read_data_stream(self):
        return Endpoint(self.data_shape(), src_loc_at=1)


class SinglePortMemory(SimpleElaboratable):
    """A single port memory with stream interfaces.

    Data is written via the memory's write_input, which has a payload of
    the data to write and the address to write it to.

    Data is read via the memory's read_addr_input and read_data_output.
    For each address sent to the input, one word is returned on the
    output.

    Writes have priority over reads. The read addr input will not become
    ready if there is a write in progress or unread data waiting to be
    transferred. 
    
    TODO: change so that reads do on combinatorially block on writes - it
    introduces a potentially long combinatorial path.

    Parameters
    ----------
    params: MemoryParameters
      Describes the memory.

    Attributes
    ----------

    write_input: Stream[addr, data], in
      For each packet sent on this sink, one word will be written to
      the memory.

    read_addr_input: Stream[addr], in
      For each packet sent on this sink, one word will be read.

    read_data_output: Stream[data], out

    TODO: This same interface would work for pseudo-dual port memory.
          Refactor.
    """

    def __init__(self, params: MemoryParameters):
        self.params = params
        self.write_input = params.make_write_stream()
        self.read_addr_input = params.make_read_addr_stream()
        self.read_data_output = params.make_read_data_stream()

    def elab(self, m: Module):
        memory = Memory(depth=self.params.depth, width=self.params.width)
        m.submodules["read_port"] = read_port = memory.read_port(
            transparent=False)
        m.submodules["write_port"] = write_port = memory.write_port()

        # Write sink always ready to write - set address and data directly
        # to memory.
        m.d.comb += self.write_input.ready.eq(1)
        with m.If(self.write_input.is_transferring()):
            m.d.comb += [
                write_port.en.eq(1),
                write_port.addr.eq(self.write_input.payload.addr),
                write_port.data.eq(self.write_input.payload.data),
            ]

        m.d.comb += read_port.en.eq(~write_port.en)
        m.d.comb += read_port.addr.eq(self.read_addr_input.payload)

        # Remember whether we got a new address
        read_addr_did_transfer = Signal()
        m.d.sync += read_addr_did_transfer.eq(
            self.read_addr_input.is_transferring())

        # If we got a new address last cycle, then we have new data on read_port.data.
        # Save data and mark it valid, if it was not immediately transferred
        # out the source
        saved_data = Signal.like(self.read_data_output.payload)
        saved_data_valid = Signal()
        with m.If(read_addr_did_transfer):
            m.d.sync += [
                saved_data.eq(read_port.data),
                saved_data_valid.eq(~self.read_data_output.is_transferring())
            ]

        # If we got a new address last cycle, then we have new data on read_port.data.
        # Present it on read_data_output and assert source valid.
        # Otherwise present saved_data.
        with m.If(read_addr_did_transfer):
            m.d.comb += self.read_data_output.payload.eq(read_port.data)
            m.d.comb += self.read_data_output.valid.eq(1)
        with m.Else():
            m.d.comb += self.read_data_output.payload.eq(saved_data)
            m.d.comb += self.read_data_output.valid.eq(saved_data_valid)

        # If data transferred out without being new data, mark saved data not
        # valid
        with m.If(~read_addr_did_transfer & self.read_data_output.is_transferring()):
            m.d.sync += saved_data_valid.eq(0)

        # Can accept read address iff not writing and not have read data
        # already waiting, unless transferring it out this cycle
        m.d.comb += self.read_addr_input.ready.eq(~self.write_input.is_transferring() & (
            self.read_data_output.is_transferring() | ~self.read_data_output.valid))
