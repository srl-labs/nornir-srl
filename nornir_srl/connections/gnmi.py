from typing import TYPE_CHECKING, Any, Dict, Optional

from pygnmi.client import gNMIclient

from nornir.core.plugins.connections import ConnectionPlugin
from nornir.core.configuration import Config

from nornir_srl.exceptions import *

class gNMI:
    def open(
            self,
            hostname: Optional[str],
            username: Optional[str],
            password: Optional[str],
            port: Optional[int],
            platform: Optional[str],
            extras: Optional[Dict[str, Any]] = None,
            configuration: Optional[Config] = None,
    ) -> None:
        """
        Open a gNMI connection to a device
        """
        target = (hostname, port)
        connection = gNMIclient(
                target=target,
                username=username,
                password=password,
                **extras
                )
        connection.connect()
        self.connection = connection
        self.capabilities = self.connection.capabilities()

    def close(self) -> None:
        self.connection.close()

