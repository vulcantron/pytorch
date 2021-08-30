import functools
import warnings

from torch.utils.data import IterDataPipe, functional_datapipe
from typing import Any, Iterator, Optional, Sized, Tuple, TypeVar, Deque
from collections import deque

T_co = TypeVar('T_co', covariant=True)


@functional_datapipe('concat')
class ConcaterIterDataPipe(IterDataPipe):
    r""" :class:`ConcaterIterDataPipe`.

    Iterable DataPipe to concatenate multiple Iterable DataPipes.
    args:
        datapipes: Iterable DataPipes being concatenated
    """
    datapipes: Tuple[IterDataPipe]
    length: Optional[int]

    def __init__(self, *datapipes: IterDataPipe):
        if len(datapipes) == 0:
            raise ValueError("Expected at least one DataPipe, but got nothing")
        if not all(isinstance(dp, IterDataPipe) for dp in datapipes):
            raise TypeError("Expected all inputs to be `IterDataPipe`")
        self.datapipes = datapipes  # type: ignore[assignment]
        self.length = None

    def __iter__(self) -> Iterator:
        for dp in self.datapipes:
            for data in dp:
                yield data

    def __len__(self) -> int:
        if self.length is not None:
            if self.length == -1:
                raise TypeError("{} instance doesn't have valid length".format(type(self).__name__))
            return self.length
        if all(isinstance(dp, Sized) for dp in self.datapipes):
            self.length = sum(len(dp) for dp in self.datapipes)
        else:
            self.length = -1
        return len(self)


# This is fake class to show API, going to be replaced by the copy from torchdata
# TODO(VitalyFedyunin): Replace with valid version, documentation and tests
class IterateBuffer(IterDataPipe):

    def __init__(self, buffer):
        self.buffer = buffer

    def __iter__(self):
        for i in self.buffer:
            yield i


@functional_datapipe('fork')
class ForkerIterDataPipe(IterDataPipe):
    r""" :class:`ForkerIterDataPipe`.

        Iterable DataPipe to create multiple instances of the same Iterable DataPipe.
        args:
            datapipe: Iterable DataPipe being copied
            num_instances: number of instances of the datapipe to create
            buffer_size: this restricts how far ahead the leading child DataPipe
             can read relative to the slowest child DataPipe
    """
    def __new__(cls, datapipe: IterDataPipe, num_instances: int, buffer_size: int = 1000):
        container = _ForkerIterDataPipe(datapipe, num_instances, buffer_size)
        return [_ChildDataPipe(container, i) for i in range(num_instances)]


class _ForkerIterDataPipe(IterDataPipe):
    r""" :class:`_ForkerIterDataPipe`.

        Container to hold instance-specific information on behalf of ForkerIterDataPipe. It tracks
        the state of its child DataPipes, maintains the buffer, and yields the next value
        as requested by the child DataPipes.
    """
    def __init__(self, datapipe: IterDataPipe, num_instances: int, buffer_size: int = 1000):
        self.main_datapipe = datapipe
        self._datapipe_iterator: Optional[Iterator[Any]] = None
        self.num_instances = num_instances
        self.buffer: Deque = deque()
        self.buffer_size = buffer_size
        self.child_pointers = [0] * num_instances  # Indicate the indices of the next element to get
        self.slowest_ptr = 0
        self.leading_ptr = 0
        self.end_ptr: Optional[int] = None

    def get_next_element_by_instance(self, instance_id: int):
        if self._datapipe_iterator is None:
            self._datapipe_iterator = iter(self.main_datapipe)
        while self.end_ptr is None or self.child_pointers[instance_id] < self.end_ptr:
            if not self.buffer or self.child_pointers[instance_id] > self.leading_ptr:
                self.leading_ptr = self.child_pointers[instance_id]
                if self.leading_ptr - self.slowest_ptr + 1 > self.buffer_size:
                    raise BufferError("ForkerIterDataPipe buffer overflow," +
                                      f"buffer size {self.buffer_size} is insufficient.")
                try:
                    self.buffer.append(next(self._datapipe_iterator))
                    self.child_pointers[instance_id] += 1
                    yield self.buffer[-1]
                except StopIteration:
                    self.end_ptr = self.leading_ptr
            else:  # Child pointer is slower than or equal to the leading_ptr
                buffer_index = self.child_pointers[instance_id] - self.slowest_ptr
                return_val = self.buffer[buffer_index]
                self.child_pointers[instance_id] += 1
                if self.child_pointers[instance_id] - 1 == self.slowest_ptr:
                    new_min = min(self.child_pointers)  # Can optimize by avoiding the call to min()
                    if self.slowest_ptr < new_min:
                        self.slowest_ptr = new_min
                        self.buffer.popleft()
                yield return_val

    def is_instance_started(self, instance_id: int) -> bool:
        return self.child_pointers[instance_id] != 0

    def is_every_instance_exhausted(self) -> bool:
        return all(self.end_ptr == ptr for ptr in self.child_pointers)

    def reset(self):
        self._datapipe_iterator = iter(self.main_datapipe)
        self.buffer = deque()
        self.child_pointers = [0] * self.num_instances
        self.slowest_ptr = 0
        self.leading_ptr = 0
        self.end_ptr = None

class _ChildDataPipe(IterDataPipe):
    r""" :class:`_ChildDataPipe`.

        Iteratable Datapipe that is a child of a main DataPipe. The instance of this class
        will pass its instance_id to get the next value from its main DataPipe.
        args:
            main_datapipe: Main DataPipe with a method 'get_next_element_by_instance(instance_id)'
            instance_id: integer identifier of this instance
    """
    def __init__(self, main_datapipe, instance_id: int):
        required_attrs = ["get_next_element_by_instance", "is_instance_started", "is_every_instance_exhausted", "reset"]
        required_ops = [getattr(main_datapipe, attr) for attr in required_attrs]
        if any(not callable(op) for op in required_ops):
            raise NotImplementedError(f"Main Datapipe must have methods {required_attrs} implemented.")
        self.main_datapipe = main_datapipe
        self.instance_id = instance_id

    def __iter__(self):
        if self.main_datapipe.is_instance_started(self.instance_id):  # Only reset if the DataPipe started to read
            if not self.main_datapipe.is_every_instance_exhausted():
                warnings.warn("Some child DataPipes are not exhausted when __iter__ is called. We are resetting "
                              "the buffer and each child DataPipe will read from the start again.", UserWarning)
            self.main_datapipe.reset()
        # We want to separate the code for reset and yield, so that 'reset' exeutes before __next__ is called
        return self.get_generator_by_instance(self.instance_id)

    def get_generator_by_instance(self, instance_id: int):
        yield from self.main_datapipe.get_next_element_by_instance(self.instance_id)


@functional_datapipe('demux')
class DemultiplexerIterDataPipe(IterDataPipe):

    def __new__(cls, datapipe, instances, classifier_fn):
        result = []
        buffer = list(datapipe)

        def filter_fn(classifier_fn, i, x):
            return classifier_fn(x) == i
        return [IterateBuffer(buffer).filter(functools.partial(filter_fn, classifier_fn, i)) for i in range(instances)]

@functional_datapipe('mux')
class MultiplexerIterDataPipe(IterDataPipe):

    def __init__(self, *datapipes):
        self.datapipes = datapipes

    def __iter__(self):
        iterators = [iter(x) for x in self.datapipes]
        finished = {}
        had_more = True
        while had_more:
            had_more = False
            for i in range(len(iterators)):
                if i not in finished:
                    try:
                        value = iterators[i].__next__()
                        had_more = True
                        yield value
                    except StopIteration:
                        finished[i] = 1


@functional_datapipe('zip')
class ZipperIterDataPipe(IterDataPipe[Tuple[T_co]]):
    r""" :class:`ZipIterDataPipe`.

    Iterable DataPipe aggregates elements into a tuple from each of
    the input DataPipe. The output DataPipe is stopped when the
    shortest input DataPipe is exhausted.
    args:
        *datapipes: Iterable DataPipes being aggregated
    """
    datapipes: Tuple[IterDataPipe]
    length: Optional[int]

    def __init__(self, *datapipes: IterDataPipe):
        if not all(isinstance(dp, IterDataPipe) for dp in datapipes):
            raise TypeError("All inputs are required to be `IterDataPipe` "
                            "for `ZipIterDataPipe`.")
        super().__init__()
        self.datapipes = datapipes  # type: ignore[assignment]
        self.length = None

    def __iter__(self) -> Iterator[Tuple[T_co]]:
        for data in zip(*self.datapipes):
            yield data

    def __len__(self) -> int:
        if self.length is not None:
            if self.length == -1:
                raise TypeError("{} instance doesn't have valid length".format(type(self).__name__))
            return self.length
        if all(isinstance(dp, Sized) for dp in self.datapipes):
            self.length = min(len(dp) for dp in self.datapipes)
        else:
            self.length = -1
        return len(self)
