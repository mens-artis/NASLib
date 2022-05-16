#%%bash
optimizer=re
predictors=(mlp lgb xgb rf bayes_lin_reg gp)

start_seed=0

# folders:
# this supposes your location is at NASLib/docs. Change the base_file location based on where you
# opened the notebook
base_file=../naslib
save_dir=reg_evo_run_0
out_dir=$save_dir\_$start_seed

# search space / data:
search_space=nasbench201
dataset=cifar10
search_epochs=300

# trials / seeds:
trials=3
end_seed=$(($start_seed + $trials - 1))

# create config files
for i in $(seq 0 $((${#predictors[@]}-1)) )
do
    predictor=${predictors[$i]}
    python3 $base_file/benchmarks/create_configs.py --predictor $predictor \
    --epochs $search_epochs --start_seed $start_seed --trials $trials \
    --out_dir $out_dir --dataset=$dataset --config_type predictor \
    --search_space $search_space --optimizer $optimizer
done
# line 30 was    --out_dir $out_dir --dataset=$dataset --config_type nas_predictor \
