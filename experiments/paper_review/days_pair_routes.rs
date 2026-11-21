// Read routes from the original executable image without changing the Days model.
use days::scenario::compile_config;
use std::env;

fn main() {
    let input = env::args().nth(1).expect("Days scenario TOML");
    let image = compile_config(input).expect("compile frozen Days workload");
    println!("flow,source,target,forward,reverse");
    for flow in &image.flows {
        let forward = flow.route.iter().map(|link| link.0.to_string()).collect::<Vec<_>>();
        let reverse = flow.reverse_route.iter().map(|link| link.0.to_string()).collect::<Vec<_>>();
        println!("{},{},{},{},{}", flow.id.0, flow.source.0, flow.target.0,
                 forward.join(" "), reverse.join(" "));
    }
    eprintln!("flows={} directed_links={}", image.flows.len(), image.links.len());
    assert!(image.links.iter().all(|link| link.rate_bps == 100_000_000_000 && link.propagation_ns == 1000));
}
