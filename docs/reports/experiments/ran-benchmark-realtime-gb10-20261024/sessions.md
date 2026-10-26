# Sessions

Every tagged single-cell session of the campaign (Spark, `/workspace/ocudu-spark-ranb/results/single-cell-scheduler-benchmark/tagged-*`), oldest first. `rt` is the two policy runs' `real_time_ratio`; gates are RUN VALID / TRACE / TRAFFIC / RADIO ALIGNMENT (P = pass). The cpus column is what `run-parameters.json` recorded (per-UE pins `SINGLE_CELL_CPUS_UE<i>` are not in it); the knobs column is the run's `local-knobs.log` (`broker_env` = `OCG_BROKER_SPIN`). Sessions before 2026-10-24 12:14 UTC ran the pinned scalar LDPC decoder (g38-g52 the SIMDe build), later ones the NEON decoder.

| session | UEs | rt test1 / test2 | gates | cpus | knobs |
|---|---:|---|---|---|---|
| base-20261022T081054Z-g1 | 4 | - / 0.406 | PPFF | gnb 5-12 broker 13 ues 14-19 | ue_rx_sleep=true log_level=default pcap=on broker_env=unset |
| rxsleep-20261022T081708Z-g2 | 4 | 0.412 / 0.398 | PPPP | gnb 5-12 broker 13 ues 14-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=unset |
| place-20261022T090434Z-g4 | 4 | 0.547 / 0.573 | PPPP | gnb 5-9 broker 15-16 ues 17-19 | ue_rx_sleep=true log_level=default pcap=on broker_env=1 |
| place-rxsleep-20261022T090858Z-g5 | 4 | 0.550 / 0.579 | PPPP | gnb 5-9 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| place-rxsleep-quiet-20261022T091321Z-g6 | 4 | - / 0.597 | FPFF | gnb 5-9 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=warning pcap=off broker_env=1 |
| ue1-place-rxsleep-20261022T092037Z-g7 | 1 | 0.898 / 0.867 | PPPP | gnb 5-9 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| place-rxsleep-hop-20261022T092322Z-g8 | 4 | 0.550 / - | FPFF | gnb 5-9 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| lphy-place-rxsleep-20261022T092934Z-g9 | 4 | - / 0.683 | FPFF | gnb 5-9 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| ue1-lphy-place-rxsleep-20261022T093518Z-g10 | 1 | 0.902 / 0.861 | FPPP | gnb 5-9 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| ue2-place-20261022T102152Z-g11 | 2 | 0.797 / 0.727 | PPPP | gnb 5-9 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| ue8-place-20261022T103947Z-g12 | 8 | - / - | FPFF | gnb 5-9 broker 15-16 ues 17-19,10-14 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| ue16-place-20261022T105017Z-g13 | 16 | - / - | FPFF | gnb 5-9 broker 15-16 ues 17-19,10-14,2-4 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| ue4-g8-20261022T111425Z-g14 | 4 | 0.561 / 0.570 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| ue8-g8-20261022T111848Z-g15 | 8 | 0.396 / 0.394 | PPPP | gnb 5-12 broker 15-16 ues 17-19,13-14 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| ue16-g8-20261022T112525Z-g16 | 16 | 0.308 / 0.308 | PPPP | gnb 5-12 broker 15-16 ues 17-19,13-14,2-4 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iso-base-20261022T115629Z-g17 | 4 | 0.578 / 0.554 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iso-keeper-20261022T120053Z-g18 | 4 | 0.393 / 0.351 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iso-light-20261022T120634Z-g19 | 4 | 0.969 / 0.968 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iso-light-keeper-20261022T120956Z-g20 | 4 | 0.486 / 0.458 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iso-base-rt-20261022T121450Z-g21 | 4 | 0.571 / 0.559 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iso-load25-20261022T121921Z-g22 | 4 | 0.745 / 0.731 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iso-load50-20261022T122310Z-g23 | 4 | 0.621 / 0.623 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iso-base-rt2-20261022T122730Z-g24 | 4 | 0.580 / 0.571 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iso-ue4core-20261022T123152Z-g25 | 4 | 0.593 / 0.556 | PPPP | gnb 5-12 broker 15 ues 16-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iso-rtio-20261022T123613Z-g26 | 4 | 0.544 / 0.559 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iso-rtio-gnb-20261023T050638Z-g27 | 4 | 0.565 / 0.564 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| trace-mixed-20261023T064518Z-g29 | 4 | 0.532 / 0.542 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| trace-1ue-20261023T064947Z-g30 | 1 | 0.863 / 0.871 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| trace-light-20261023T065232Z-g31 | 4 | 0.963 / 0.969 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| trace2-mixed-20261023T065606Z-g32 | 4 | 0.566 / 0.570 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iter4-mixed-20261023T070030Z-g33 | 4 | 0.634 / 0.637 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| iter4-1ue-20261023T070439Z-g34 | 1 | 0.929 / 0.908 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| trace3-mixed-20261023T070732Z-g35 | 4 | 0.570 / 0.550 | PPPF | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| trace4-1ue-20261023T071325Z-g36 | 1 | 0.859 / 0.651 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| trace4-mixed-20261023T071624Z-g37 | 4 | 0.571 / 0.534 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| simde-1ue-20261023T084217Z-g38 | 1 | 0.989 / 0.978 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| simde-4ue-b-20261023T090948Z-g40 | 4 | - | ---- | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| simde-8ue-20261023T091034Z-g41 | 8 | 0.582 / 0.580 | PPPF | gnb 5-12 broker 15-16 ues 13-14,17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| simde-lphy-4ue-20261023T094120Z-g43 | 4 | - | ---- | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| simde-lphy-8ue-20261023T094207Z-g44 | 8 | 0.634 / - | FPFF | gnb 5-12 broker 15-16 ues 13-14,17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| simde-4ue-d-20261023T101549Z-g46 | 4 | - | ---- | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| simde-4ue-e-20261023T102230Z-g47 | 4 | 0.938 / 0.896 | PPPF | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| simde-2ue-20261024T105626Z-g48 | 2 | 0.972 / 0.982 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| simde-16ue-20261024T105917Z-g49 | 16 | 0.362 / 0.349 | FPPP | gnb 5-12 broker 15-16 ues 17-19,13-14,2-4 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| simde-4ue-f-20261024T110846Z-g50 | 4 | 0.878 / 0.897 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 |
| simde-stock-4ue-20261024T112056Z-g51 | 4 | 0.448 / 0.456 | PPPP | gnb 5-12 broker 13 ues 14-19 | ue_rx_sleep=true log_level=default pcap=on broker_env=unset |
| simde-stock-1ue-20261024T112556Z-g52 | 1 | 0.926 / 0.833 | PPPP | gnb 5-12 broker 13 ues 14-19 | ue_rx_sleep=true log_level=default pcap=on broker_env=unset |
| repin-8ue-20261024T114251Z-g53 | 8 | - / 0.710 | FPFF | gnb 5-9 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=5 |
| repin-8ue-phy2-20261024T115030Z-g54 | 8 | - / 0.711 | FPFF | gnb 5-9 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=2 gnb_main_pool=5 |
| ahead3-4ue-20261024T120101Z-g55 | 4 | 0.968 / 0.961 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=auto |
| ahead3-strict-4ue-20261024T120429Z-g56 | 4 | 0.955 / 0.947 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=auto |
| ahead3-repin-8ue-20261024T120756Z-g57 | 8 | 0.691 / 0.695 | PPFP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=auto |
| neon-4ue-20261024T121400Z-g58 | 4 | 0.993 / 0.995 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=auto |
| neon-8ue-repin-20261024T121723Z-g59 | 8 | 0.782 / 0.779 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=auto |
| neon-8ue-repin-ahead3-20261024T124726Z-g61 | 8 | 0.781 / 0.812 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=auto |
| neon-16ue-repin-20261024T125220Z-g62 | 16 | 0.373 / 0.361 | FPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=auto |
| neon-8ue-repin-clean-20261024T130534Z-g63 | 8 | 0.776 / 0.776 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=auto |
| neon-8ue-repin-ahead3-dual-20261024T131028Z-g64 | 8 | - / - | FPFF | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=auto |
| neon-4ue-ahead3-20261024T132056Z-g65 | 4 | 0.998 / 0.997 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=auto |
| neon-16ue-repin-ahead3-dual-20261024T132419Z-g66 | 16 | - / - | FPFF | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=on broker_env=1 ue_phy_threads=3 gnb_main_pool=auto |
| quiet-8ue-repin-20261024T134158Z-g67 | 8 | 0.786 / 0.801 | PPFP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| quiet-8ue-repin-ahead3-20261024T134644Z-g68 | 8 | 0.807 / 0.816 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| quiet-8ue-gnb7-ahead3-20261024T135129Z-g69 | 8 | 0.420 / 0.454 | PPPP | gnb 5-9,15-16 broker 10-11 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| quiet-4ue-ahead3-20261024T135741Z-g70 | 4 | 0.998 / 0.998 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| quiet-16ue-repin-ahead3-20261024T140100Z-g71 | 16 | 0.375 / 0.396 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| gnbk-8ue-singlecopy-20261024T141433Z-g72 | 8 | 0.811 / 0.815 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| gnbk-8ue-lphysingle-20261024T141918Z-g73 | 8 | - / 0.603 | FPFF | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| gnbk-8ue-rtprio-20261024T142628Z-g74 | 8 | 0.804 / 0.796 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| gnbk-8ue-dual-rt-20261024T143136Z-g75 | 8 | - / - | ---- | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| brk4-8ue-20261024T144225Z-g76 | 8 | 0.501 / 0.498 | PPPP | gnb 5-12 broker 15-18 ues 19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| brk3-8ue-20261024T144812Z-g77 | 8 | - | ---- | gnb 5-12 broker 15-17 ues 18-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| gnbk-8ue-single-rt-20261024T145052Z-g78 | 8 | - / - | ---- | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| rupin-8ue-20261024T150106Z-g79 | None | - / - | FPFF | gnb None broker None ues None | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| nospin2-8ue-20261024T150158Z-g80 | 8 | 0.874 / 0.886 | PPFF | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=unset ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| nospin4-8ue-20261024T150633Z-g81 | 8 | 0.472 / 0.503 | FPPP | gnb 5-12 broker 15-18 ues 19 | ue_rx_sleep=false log_level=default pcap=off broker_env=unset ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| spin20-4c-8ue-20261024T151223Z-g82 | 8 | 0.480 / 0.487 | PPPP | gnb 5-12 broker 15-18 ues 19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| io2-4c-8ue-20261024T151840Z-g83 | 8 | 0.487 / 0.499 | PPPP | gnb 5-12 broker 15-18 ues 19 | ue_rx_sleep=false log_level=default pcap=off broker_env=1 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| io4-spin2-4c-8ue-20261024T152429Z-g84 | 8 | 0.504 / 0.505 | PPPP | gnb 5-12 broker 15-18 ues 19 | ue_rx_sleep=false log_level=default pcap=off broker_env=2 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| io2-spin2-2c-8ue-20261024T153015Z-g85 | 8 | 0.933 / 0.930 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=2 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| rupin2-8ue-20261024T153503Z-g86 | None | - / - | FPFF | gnb None broker None ues None | ue_rx_sleep=false log_level=default pcap=off broker_env=unset ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| brk3b-8ue-20261024T153529Z-g87 | 8 | 0.668 / 0.668 | PPPP | gnb 5-12 broker 15-17 ues 18-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=unset ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| rxsleep-8ue-20261024T154033Z-g88 | 8 | 0.865 / 0.845 | PPFF | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=true log_level=default pcap=off broker_env=unset ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| best-8ue-20261024T154511Z-g89 | 8 | 0.883 / 0.891 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=unset ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| spin2-8ue-20261024T154946Z-g90 | 8 | 0.928 / 0.940 | FPFF | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=2 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| nospin-16ue-20261024T155443Z-g91 | 16 | 0.385 / 0.389 | FPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=unset ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| nospin-4ue-20261024T160339Z-g92 | 4 | 0.998 / 0.998 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=unset ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| nospin-8ue-notrace-20261024T160657Z-g93 | 8 | 0.903 / 0.883 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=unset ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| s2io4-8ue-20261024T161149Z-g94 | 8 | 0.942 / 0.932 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=2 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| s2io2-rxs-8ue-20261024T161619Z-g95 | 8 | 0.884 / 0.886 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=true log_level=default pcap=off broker_env=2 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| s2io2-16ue-20261024T162055Z-g96 | 16 | 0.460 / 0.460 | PPPF | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=2 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| s2io2-4ue-20261024T162914Z-g97 | 4 | 0.998 / 0.998 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=2 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| s2io2-8ue-b-20261024T163232Z-g98 | 8 | 0.930 / 0.942 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=2 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| t-idle5-8ue-20261024T163722Z-g99 | 8 | 0.930 / 0.929 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=2 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| t-phy2-8ue-20261024T164156Z-g100 | 8 | 0.940 / 0.927 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=2 ue_phy_threads=2 gnb_main_pool=auto quiet_logs=1 |
| t-brk1-8ue-20261024T164626Z-g101 | 8 | 0.544 / 0.533 | PPPP | gnb 5-12 broker 15 ues 16-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=0 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |
| t-big3-8ue-20261024T165201Z-g102 | 8 | 0.713 / 0.713 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false log_level=default pcap=off broker_env=2 ue_phy_threads=3 gnb_main_pool=auto quiet_logs=1 |

Verification of the committed lock build (2026-10-26, `build-srsue-local.sh` with the lock as committed, srsue sha256 `0e32b703…`, no slot-trace instrumentation), the recipe through `SINGLE_CELL_GNB_BINARY`:

| session | UEs | rt test1 / test2 | gates | cpus | knobs |
|---|---:|---|---|---|---|
| lock-4ue-20261026T055524Z-g103 | 4 | 0.998 / 0.999 | PPPP | gnb 5-12 broker 15-16 ues 17-19 | ue_rx_sleep=false pcap=off broker_env=2 quiet_logs=1 gnb_binary=ocudu-lphy-release rx_to_tx_max_ms=3 |
| lock-8ue-20261026T055841Z-g104 | 8 | 0.943 / 0.948 | PPPP | gnb 5-12 broker 15-16 ues 17-19 (+UE6/7 on 13/14) | same |
