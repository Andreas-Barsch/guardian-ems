"""HA add-on entry point; /config read-only, simulation archive under /data."""
import json
import os
from pathlib import Path
options=json.loads(Path('/data/options.json').read_text())
config=json.loads(Path(options['config_file']).read_text())
if config.get('sources',{}).get('guardian_shared'):
    config['sources']['guardian_shared']['cache']='/data/guardian-cache.sqlite'
if config.get('solar_learning',{}).get('enabled'):
    config['solar_learning']['state_file']='/data/solar-learning.sqlite'
Path('/data/planner.runtime.json').write_text(json.dumps(config))
os.execvp('python',['python','-m','hycube_sim','serve','--config','/data/planner.runtime.json',
                    '--output','/data/simulations','--interval','900','--duration-hours','0','--host','0.0.0.0','--port','8091'])
