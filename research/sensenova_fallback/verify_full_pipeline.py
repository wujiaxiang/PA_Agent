"""Real free-upstream acceptance with production PA prompts/validation.
Only primary failure is injected; synthetic bars; never starts GUI or trading.
Run from any cwd. Every run has its own log, prompts and complete record.
"""
from pathlib import Path
import sys, json, time, logging, uuid

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT))
from pa_agent.config.settings import Settings
from pa_agent.ai.deepseek_client import DeepSeekClient
from pa_agent.ai.prompt_assembler import PromptAssembler
from pa_agent.ai.json_validator import JsonValidator
from pa_agent.ai.router import route_strategy_files
from pa_agent.orchestrator.two_stage import TwoStageOrchestrator
from pa_agent.records.pending_writer import PendingWriter
from pa_agent.records.experience_reader import ExperienceReader
from pa_agent.util.threading import CancelToken
from pa_agent.data.base import KlineBar, KlineFrame, IndicatorBundle
from tests.integration.conftest import make_frame


def main():
    out = Path(__file__).resolve().parent / 'runs' / (
        'full-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    out.mkdir(parents=True)
    logging.basicConfig(level=logging.INFO, handlers=[
        logging.FileHandler(out / 'run.log', encoding='utf-8'), logging.StreamHandler()])
    settings = Settings()
    settings.provider.base_url = 'https://token.sensenova.cn/v1'
    settings.provider.api_key = 'synthetic-primary-key-not-real'
    exp = ExperienceReader(out / 'empty-experience')
    client = DeepSeekClient(settings.provider)
    def primary_failure(*args, **kwargs):
        raise ConnectionError('Injected primary network failure; free upstream remains real')
    client.stream_chat = primary_failure
    orchestrator = TwoStageOrchestrator(
        client, PromptAssembler(PROJECT / 'prompt_engineering', exp,
                                prompt_settings=settings.prompt),
        route_strategy_files, JsonValidator(settings.validation),
        PendingWriter(out / 'records'), exp, settings)
    frame = make_frame()
    trend = '--trend' in sys.argv
    if trend:
        n = 60
        bars = tuple(KlineBar(
            seq=i+1, ts_open=1791586800000-i*3600000,
            open=2000+(n-1-i)*2, high=2002+(n-1-i)*2, low=1999.8+(n-1-i)*2,
            close=2001.8+(n-1-i)*2, volume=100, closed=True) for i in range(n))
        frame = KlineFrame('SYNTHETIC_XAUUSD', '1h', bars,
            IndicatorBundle(tuple(b.close-15 for b in bars), tuple(2.2 for b in bars)),
            1791590400000)
    events = []
    progress: list[str] = []
    start = time.monotonic()
    try:
        record = orchestrator.submit(
            frame, CancelToken(), lambda e: events.append(e.name),
            on_stage1_content=progress.append,
            on_stage2_content=progress.append,
        )
        (out / 'progress.txt').write_text(''.join(progress), encoding='utf-8')
        (out / 'record.json').write_text(
            json.dumps(record.model_dump(mode='json'), ensure_ascii=False, indent=2), encoding='utf-8')
        complete = record.exception is None and record.stage1_diagnosis is not None and record.stage2_decision is not None
        called = record.stage2_response is not None
        summary = {'ok': complete and (called or not trend),
            'analysis_complete': complete, 'stage2_model_called': called,
            'events': events, 'exception': record.exception,
            'fallback_hints_shown': sum(1 for c in progress if '[提示]' in c),
            'fallback_hint_bytes': sum(len(c) for c in progress if '[提示]' in c),
            'elapsed_s': time.monotonic()-start,
            'note': 'Production prompts/validator/writer; synthetic bars; injected primary failure; REAL free upstream. A legitimate wait is not a product failure.'}
    except Exception as exc:
        logging.exception('Full pipeline failed')
        summary = {'ok': False, 'events': events, 'error': str(exc), 'elapsed_s': time.monotonic()-start}
    (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('OUT=' + str(out))
    return 0 if summary['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
