"""Word backend 兼容 facade；流程控制位于 orchestration。"""

from ..orchestration.scheduler import verify_claim, verify_claim_document

__all__ = ["verify_claim", "verify_claim_document"]
