from typing import Dict, List, Optional

from asgidav.member import Member

MaybeMember = Optional[Member]


class FSCache:
    """Tree of cached members, one node per path component.

    Only ``set`` creates nodes. Lookups and resets walk the nodes that
    already exist, so a request for a path that was never cached -- or a
    reset after writing to a new path -- does not leave an empty node
    behind for the rest of the process lifetime.
    """

    def __init__(self, value: MaybeMember = None):
        self._cache: Dict[str, FSCache] = {}
        self._value: MaybeMember = value

    @staticmethod
    def split_path(path: str) -> List[str]:
        path = path.strip("/")
        if path == "":
            return [""]
        return ["", *path.split("/")]

    def __find(self, parts: List[str]) -> Optional["FSCache"]:
        node: Optional[FSCache] = self
        for part in parts:
            if node is None:
                return None
            node = node._cache.get(part)
        return node

    def get(self, parts: str) -> MaybeMember:
        node = self.__find(self.split_path(parts))
        return node._value if node is not None else None

    def __set(self, path: List[str], value: MaybeMember):
        if len(path) == 1:
            self._cache[path[0]] = FSCache(value)
        else:
            self._cache.setdefault(path[0], FSCache()).__set(path[1:], value)

    def set(self, path: str, value: MaybeMember):
        self.__set(self.split_path(path), value)

    def __drop(self, parts: List[str]) -> None:
        """Forget the node at ``parts`` together with everything below it."""
        if not parts:
            self._cache.clear()
            return
        if (parent := self.__find(parts[:-1])) is not None:
            parent._cache.pop(parts[-1], None)

    def reset(self, path: str):
        self.__drop(self.split_path(path))

    def reset_parent(self, path: str):
        self.__drop(self.split_path(path)[:-1])


gfc: Dict[str, FSCache] = {}
