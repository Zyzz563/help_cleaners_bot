from .commands import router as commands_router
from .photos import router as photos_router
from .vpn import router as vpn_router

__all__ = [
	"commands_router",
	"photos_router",
	"vpn_router",
] 