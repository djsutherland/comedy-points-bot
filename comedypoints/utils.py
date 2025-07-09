from collections import OrderedDict


# functools.lru_cache API is too limited
class LRUCache:
    def __init__(self, max_size=128):
        self.max_size = max_size
        self._items = OrderedDict()

    def touch(self, key):
        self._items.move_to_end(key, last=False)

    def __contains__(self, key):
        return key in self._items

    def __getitem__(self, key):
        if key in self._items:
            self.touch(key)
        return self._items[key]

    def __setitem__(self, key, value):
        if key not in self._items:
            while len(self._items) >= self.max_size:
                self._items.popitem(last=True)
        else:
            self.touch(key)
        self._items[key] = value

    def __delitem__(self, key):
        del self._items[key]
