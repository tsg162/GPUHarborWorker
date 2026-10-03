"""Small real CUDA training run exercising GPUHarbor checkpoints and resume."""
import argparse
import json
import os
import time
from pathlib import Path
import torch
from gpuharbor_training import checkpoint_complete

parser = argparse.ArgumentParser()
parser.add_argument('--steps', type=int, default=100)
args = parser.parse_args()
torch.manual_seed(42)
device = torch.device('cuda')
model = torch.nn.Sequential(torch.nn.Linear(32, 128), torch.nn.ReLU(), torch.nn.Linear(128, 1)).to(device)
optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
start = 0
resume = os.environ.get('GPUHARBOR_RESUME_DIR')
if resume:
    state = torch.load(Path(resume) / 'state.pt', map_location=device, weights_only=True)
    model.load_state_dict(state['model']); optimizer.load_state_dict(state['optimizer'])
    torch.set_rng_state(state['rng'].cpu())
    torch.cuda.set_rng_state_all([value.cpu() for value in state['cuda_rng']])
    start = state['step']
    print(f'Resuming at step {start}', flush=True)
started = time.monotonic()
for step in range(start + 1, start + args.steps + 1):
    x = torch.randn(256, 32, device=device)
    y = x.sum(dim=1, keepdim=True)
    optimizer.zero_grad(set_to_none=True)
    loss = torch.nn.functional.mse_loss(model(x), y)
    loss.backward(); optimizer.step()
    if step % 10 == 0:
        print(json.dumps({'step': step, 'loss': loss.item(), 'samples_per_sec': (step-start)*256/(time.monotonic()-started)}), flush=True)
    if step % 25 == 0 or step == start + args.steps:
        folder = Path(os.environ['GPUHARBOR_CHECKPOINT_DIR']) / f'checkpoint-{step:08d}'
        folder.mkdir()
        torch.save({'step': step, 'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'rng': torch.get_rng_state(), 'cuda_rng': torch.cuda.get_rng_state_all()}, folder / 'state.pt')
        (folder / 'metadata.json').write_text(json.dumps({'step': step, 'loss': loss.item()}))
        checkpoint_complete(folder)
torch.save(model.state_dict(), Path(os.environ['GPUHARBOR_OUTPUT_DIR']) / 'model.pt')
print('Training complete', flush=True)
